# -*- coding: utf-8 -*-
#
# Agent 流式生成任务：内存缓冲 + Redis 持久化 + 双阶段续传
#
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime
from typing import Any, AsyncIterator

from common.logger import logger
from models.db_model import ChatAgentSessionModel

from chat.generation_buffer import (
    GenerationBuffer,
    flush_buffer,
    get_buffer,
    remove_buffer,
)
from chat.message_blocks import (
    flatten_blocks,
    flatten_reasoning_blocks,
    load_blocks_from_message_metadata,
    preserve_session_assistant_metadata,
    sync_derived_from_blocks,
)
from chat.redis_client import get_chat_redis_settings
from chat.redis_generation import (
    GENERATION_STATUS_CANCELLED,
    GENERATION_STATUS_COMPLETED,
    GENERATION_STATUS_FAILED,
    GENERATION_STATUS_PAUSED,
    GENERATION_STATUS_RUNNING,
    append_chunks_batch,
    clear_session_running,
    create_generation_meta,
    delete_generation_checkpoint,
    delete_generation_payload,
    get_chunks_len,
    get_chunks_range,
    get_generation_checkpoint,
    get_generation_meta,
    get_session_running,
    notify_generation_signal,
    save_generation_checkpoint,
    save_generation_payload,
    set_session_running,
    update_generation_meta,
    wait_signal,
)
from chat.stream_format import format_chunk, format_stream_done
from chat.schemas import AgentChatRequest, ChatMessage
from chat.session_service import (
    CHAT_SESSION_STATUS_ACTIVE,
    CHAT_SESSION_STATUS_DELETED,
    _normalize_username,
    conversation_messages_to_chat_messages,
    merge_session_request_messages,
)

USER_STOPPED_GENERATION_MESSAGE = "用户已停止生成"


def _as_int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _duration_ms_between(start: str | None, end: str | None) -> int | None:
    start_s = str(start or "").strip()
    end_s = str(end or "").strip()
    if not start_s or not end_s:
        return None
    try:
        start_dt = datetime.strptime(start_s, "%Y-%m-%d %H:%M:%S")
        end_dt = datetime.strptime(end_s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return max(0, int((end_dt - start_dt).total_seconds() * 1000))


def _resolve_cancelled_output(
    *,
    content: str,
    reasoning: str,
    blocks: list[dict[str, Any]] | None,
) -> tuple[str, str, list[dict[str, Any]], bool]:
    """无正文且无思考内容时，写入停止提示。"""
    has_output = bool(content.strip()) or bool(reasoning.strip())
    if has_output:
        return content, reasoning, list(blocks or []), False
    fallback_blocks = [{"type": "text", "content": USER_STOPPED_GENERATION_MESSAGE}]
    return USER_STOPPED_GENERATION_MESSAGE, reasoning, fallback_blocks, True


def _messages_to_dicts(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in messages:
        row: dict[str, Any] = {"role": item.role, "content": item.content}
        if item.metadata:
            row["metadata"] = item.metadata
        result.append(row)
    return result


def parse_sse_delta(sse_line: str) -> dict[str, Any]:
    line = (sse_line or "").strip()
    if not line.startswith("data:"):
        return {}
    payload_text = line[5:].strip()
    if not payload_text or payload_text == "[DONE]":
        return {}
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return {}
    choices = payload.get("choices") or []
    if not choices:
        return {}
    return choices[0].get("delta") or {}


def generation_to_dict(meta: dict[str, Any]) -> dict[str, Any]:
    offset = int(meta.get("offset") or 0)
    workflow_data = meta.get("workflow_data")
    steps = meta.get("steps")
    blocks = meta.get("blocks")
    if not isinstance(workflow_data, dict):
        workflow_data = {}
    if not isinstance(steps, list):
        steps = []
    if not isinstance(blocks, list):
        blocks = []
    return {
        "generation_id": meta.get("generation_id"),
        "session_id": meta.get("session_id"),
        "message_id": meta.get("message_id"),
        "chunk_id": meta.get("chunk_id"),
        "status": meta.get("status"),
        "offset": offset,
        "content": meta.get("content") or "",
        "reasoning_content": meta.get("reasoning_content") or "",
        "mode": meta.get("mode"),
        "intent": meta.get("intent"),
        "workflow_data": workflow_data,
        "steps": steps,
        "blocks": blocks,
        "error": meta.get("error"),
        "celery_task_id": meta.get("celery_task_id"),
        "created_at": meta.get("created_at"),
        "modified_at": meta.get("modified_at"),
        "completed_at": meta.get("completed_at"),
        "question_at": meta.get("question_at"),
        "thinking_ms": _as_int_or_none(meta.get("thinking_ms")),
        "duration_ms": _as_int_or_none(meta.get("duration_ms")),
    }


async def _enrich_paused_generation(result: dict[str, Any], generation_id: str) -> None:
    workflow_data = result.get("workflow_data")
    if not isinstance(workflow_data, dict):
        workflow_data = {}
    if workflow_data.get("expires_at"):
        result["workflow_data"] = workflow_data
        return

    checkpoint = await get_generation_checkpoint(generation_id)
    if not checkpoint:
        result["workflow_data"] = workflow_data
        return

    for key in (
        "action_id",
        "action_type",
        "title",
        "kind",
        "confirm_timeout_sec",
        "paused_at",
        "expires_at",
    ):
        if checkpoint.get(key) is not None:
            workflow_data[key] = checkpoint.get(key)
    result["workflow_data"] = workflow_data


def _meta_from_buffer(buffer: GenerationBuffer) -> dict[str, Any]:
    return {
        "generation_id": buffer.generation_id,
        "session_id": buffer.session_id,
        "message_id": buffer.message_id,
        "chunk_id": buffer.chunk_id,
        "creator": buffer.creator,
        "status": buffer.status,
        "offset": len(buffer.chunks),
        "content": buffer.content,
        "reasoning_content": buffer.reasoning_content,
        "mode": buffer.mode,
        "intent": buffer.intent,
        "workflow_data": buffer.workflow_data,
        "steps": buffer.steps,
        "blocks": buffer.blocks,
        "error": buffer.error,
        "completed_at": buffer.completed_at,
        "thinking_ms": buffer.thinking_ms,
        "duration_ms": buffer.duration_ms,
    }


async def get_generation(generation_id: str, creator: str) -> dict[str, Any] | None:
    username = _normalize_username(creator)
    buffer = get_buffer(generation_id)
    if buffer and _normalize_username(buffer.creator) == username:
        result = generation_to_dict(_meta_from_buffer(buffer))
        if result.get("status") == GENERATION_STATUS_PAUSED:
            await _enrich_paused_generation(result, generation_id)
        return result

    meta = await get_generation_meta(generation_id)
    if not meta:
        return None
    if _normalize_username(meta.get("creator") or "") != username:
        return None
    result = generation_to_dict(meta)
    if result.get("status") == GENERATION_STATUS_PAUSED:
        await _enrich_paused_generation(result, generation_id)
    return result


async def find_running_generation(session_id: str, creator: str) -> dict[str, Any] | None:
    username = _normalize_username(creator)
    generation_id = await get_session_running(username, session_id)
    if not generation_id:
        return None

    meta = await get_generation(generation_id, username)
    if not meta:
        await clear_session_running(username, session_id)
        return None
    if meta.get("status") not in (GENERATION_STATUS_RUNNING, GENERATION_STATUS_PAUSED):
        await clear_session_running(username, session_id)
        return None
    return meta


async def create_generation_for_session(
    session_id: str,
    creator: str,
    payload: AgentChatRequest,
) -> dict[str, Any]:
    username = _normalize_username(creator)
    session = await ChatAgentSessionModel.filter(
        session_id=session_id,
        creator=username,
        status=CHAT_SESSION_STATUS_ACTIVE,
    ).first()
    if not session:
        raise ValueError("会话不存在或无权访问")

    existing = await find_running_generation(session_id, username)
    if existing:
        return {
            "generation_id": existing["generation_id"],
            "message_id": existing["message_id"],
            "chunk_id": existing["chunk_id"],
            "reused": True,
            "question_at": existing.get("question_at"),
        }

    message_id = str(uuid.uuid4())
    generation_id = str(uuid.uuid4())
    chunk_id = message_id
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    existing_messages = session.messages if isinstance(session.messages, list) else []
    incoming_messages = _messages_to_dicts(payload.messages)
    conversation = merge_session_request_messages(existing_messages, incoming_messages)

    from chat.memory_service import inject_session_memory_into_payload, load_session_memory

    memory = await load_session_memory(session_id)
    payload = inject_session_memory_into_payload(payload, memory)
    payload = payload.model_copy(
        update={"messages": conversation_messages_to_chat_messages(conversation)},
    )
    messages = [dict(item) for item in conversation]
    preserve_session_assistant_metadata(messages, existing_messages)
    last_user = next(
        (item for item in reversed(messages) if item.get("role") == "user"),
        None,
    )
    question_at = ""
    if isinstance(last_user, dict):
        question_at = str(last_user.get("created_at") or "").strip() or now
        last_user["created_at"] = question_at
    assistant_placeholder = {
        "role": "assistant",
        "content": "",
        "metadata": {
            "id": message_id,
            "status": "streaming",
            "generation_id": generation_id,
            "blocks": [],
        },
    }
    messages.append(assistant_placeholder)
    await ChatAgentSessionModel.filter(id=session.id).update(messages=messages)

    meta = {
        "generation_id": generation_id,
        "session_id": session_id,
        "message_id": message_id,
        "chunk_id": chunk_id,
        "creator": username,
        "status": GENERATION_STATUS_RUNNING,
        "offset": 0,
        "content": "",
        "reasoning_content": "",
        "created_at": now,
        "modified_at": now,
        "question_at": question_at,
        "started_ms": int(time.time() * 1000),
    }
    await create_generation_meta(meta)
    await set_session_running(username, session_id, generation_id)
    await save_generation_payload(generation_id, payload.model_dump(mode="json"))

    from chat.stream_format import format_generation_info_chunk

    info_line = format_generation_info_chunk(
        generation_id,
        message_id,
        chunk_id=chunk_id,
        question_at=question_at or None,
    )
    await append_chunks_batch(generation_id, [info_line], offset=1)

    from chat.context_usage_service import compute_context_usage_for_session

    context_usage = await compute_context_usage_for_session(
        session_id,
        username,
        payload,
        mode="react",
    )

    return {
        "generation_id": generation_id,
        "message_id": message_id,
        "chunk_id": chunk_id,
        "reused": False,
        "context_usage": context_usage,
        "question_at": question_at,
    }


async def append_generation_chunk(
    generation_id: str,
    sse_line: str,
    *,
    content_delta: str = "",
    reasoning_delta: str = "",
    update_blocks: bool = True,
) -> None:
    buffer = get_buffer(generation_id)
    if not buffer:
        logger.warning(f"append 时 buffer 不存在 generation_id={generation_id}")
        return
    await buffer.append(
        sse_line,
        content_delta=content_delta,
        reasoning_delta=reasoning_delta,
        update_blocks=update_blocks,
    )


async def apply_generation_block_ops(
    generation_id: str,
    ops: list[Any],
) -> None:
    buffer = get_buffer(generation_id)
    if not buffer:
        logger.warning(f"apply block_ops 时 buffer 不存在 generation_id={generation_id}")
        return
    await buffer.apply_block_ops(ops)


async def finalize_generation(
    generation_id: str,
    *,
    status: str,
    mode: str | None = None,
    intent: str | None = None,
    workflow_data: dict[str, Any] | None = None,
    steps: list[Any] | None = None,
    content: str | None = None,
    reasoning_content: str | None = None,
    blocks: list[Any] | None = None,
    error: str | None = None,
    thinking_ms: int | None = None,
    duration_ms: int | None = None,
) -> None:
    buffer = get_buffer(generation_id)
    if not buffer:
        meta = await get_generation_meta(generation_id)
        if not meta:
            return
        session_id = meta.get("session_id") or ""
        creator = meta.get("creator") or ""
    else:
        await buffer.complete(
            status,
            mode=mode,
            intent=intent,
            workflow_data=workflow_data,
            steps=steps,
            content=content,
            reasoning_content=reasoning_content,
            blocks=blocks,
            error=error,
        )
        await flush_buffer(generation_id, force=True)
        session_id = buffer.session_id
        creator = buffer.creator
        meta = _meta_from_buffer(buffer)
        redis_meta = await get_generation_meta(generation_id) or {}
        for key in ("question_at", "created_at", "thinking_ms", "duration_ms", "started_ms"):
            if meta.get(key) is None and redis_meta.get(key) is not None:
                meta[key] = redis_meta.get(key)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    update_fields: dict[str, Any] = {
        "status": status,
        "offset": len(buffer.chunks) if buffer else int(meta.get("offset") or 0),
        "modified_at": now,
    }
    if status in (GENERATION_STATUS_COMPLETED, GENERATION_STATUS_CANCELLED):
        update_fields["completed_at"] = now
        if buffer:
            buffer.completed_at = now

    prev_thinking = _as_int_or_none(meta.get("thinking_ms")) or 0
    segment_thinking = _as_int_or_none(thinking_ms)
    if segment_thinking is not None:
        total_thinking = prev_thinking + segment_thinking
        update_fields["thinking_ms"] = total_thinking
        if buffer:
            buffer.thinking_ms = total_thinking

    started_ms = _as_int_or_none(meta.get("started_ms"))
    wall_duration = None
    if started_ms is not None and status in (
        GENERATION_STATUS_COMPLETED,
        GENERATION_STATUS_CANCELLED,
    ):
        wall_duration = max(0, int(time.time() * 1000) - started_ms)
    if wall_duration is None:
        wall_duration = _duration_ms_between(
            meta.get("question_at") or meta.get("created_at"),
            now if status in (GENERATION_STATUS_COMPLETED, GENERATION_STATUS_CANCELLED) else None,
        )
    resolved_duration = wall_duration if wall_duration is not None else _as_int_or_none(duration_ms)
    if resolved_duration is not None:
        update_fields["duration_ms"] = resolved_duration
        if buffer:
            buffer.duration_ms = resolved_duration

    if mode is not None:
        update_fields["mode"] = mode
    if intent is not None:
        update_fields["intent"] = intent
    if workflow_data is not None:
        update_fields["workflow_data"] = workflow_data
    if steps is not None:
        update_fields["steps"] = steps
    if content is not None:
        update_fields["content"] = content
    if reasoning_content is not None:
        update_fields["reasoning_content"] = reasoning_content
    if blocks is not None:
        update_fields["blocks"] = blocks
    if error is not None:
        update_fields["error"] = error

    await update_generation_meta(generation_id, update_fields)
    await sync_session_assistant_from_generation(generation_id, final=True)

    if session_id and status == GENERATION_STATUS_COMPLETED:
        await schedule_session_memory_task(session_id)

    if session_id and creator:
        await clear_session_running(creator, session_id)

    if buffer:
        remove_buffer(generation_id)


async def pause_generation(
    generation_id: str,
    *,
    checkpoint: dict[str, Any],
    content: str | None = None,
    reasoning_content: str | None = None,
    mode: str | None = None,
    intent: str | None = None,
    workflow_data: dict[str, Any] | None = None,
    steps: list[Any] | None = None,
    blocks: list[Any] | None = None,
    thinking_ms: int | None = None,
    duration_ms: int | None = None,
) -> None:
    buffer = get_buffer(generation_id)
    await flush_buffer(generation_id, force=True)

    if buffer and blocks is not None:
        buffer.blocks = blocks
        buffer.content, buffer.reasoning_content = sync_derived_from_blocks(blocks)
    else:
        if buffer and content is not None:
            buffer.content = content
        if buffer and reasoning_content is not None:
            buffer.reasoning_content = reasoning_content

    await save_generation_checkpoint(generation_id, checkpoint)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    offset = len(buffer.chunks) if buffer else await get_chunks_len(generation_id)
    update_fields: dict[str, Any] = {
        "status": GENERATION_STATUS_PAUSED,
        "offset": offset,
        "modified_at": now,
    }
    if content is not None:
        update_fields["content"] = content
    if reasoning_content is not None:
        update_fields["reasoning_content"] = reasoning_content
    if mode is not None:
        update_fields["mode"] = mode
    if intent is not None:
        update_fields["intent"] = intent
    if workflow_data is not None:
        update_fields["workflow_data"] = workflow_data
    if steps is not None:
        update_fields["steps"] = steps
    if blocks is not None:
        update_fields["blocks"] = blocks

    meta = await get_generation_meta(generation_id) or {}
    prev_thinking = _as_int_or_none(meta.get("thinking_ms")) or 0
    segment_thinking = _as_int_or_none(thinking_ms)
    if segment_thinking is not None:
        total_thinking = prev_thinking + segment_thinking
        update_fields["thinking_ms"] = total_thinking
        if buffer:
            buffer.thinking_ms = total_thinking
    segment_duration = _as_int_or_none(duration_ms)
    if segment_duration is not None:
        update_fields["duration_ms"] = segment_duration
        if buffer:
            buffer.duration_ms = segment_duration

    await update_generation_meta(generation_id, update_fields)
    await sync_session_assistant_from_generation(generation_id, final=False)

    if buffer:
        remove_buffer(generation_id)

    action_id = checkpoint.get("action_id")
    expires_at = checkpoint.get("expires_at")
    confirm_timeout_sec = checkpoint.get("confirm_timeout_sec")
    if action_id and expires_at:
        countdown = max(int(expires_at) - int(time.time()), 0)
        await schedule_expire_task(
            generation_id,
            str(action_id),
            countdown=countdown,
        )
    elif action_id and confirm_timeout_sec:
        await schedule_expire_task(
            generation_id,
            str(action_id),
            countdown=int(confirm_timeout_sec),
        )


async def respond_to_generation_action(
    generation_id: str,
    creator: str,
    *,
    action_id: str,
    approved: bool,
    draft: dict[str, Any] | None = None,
) -> dict[str, Any]:
    username = _normalize_username(creator)
    meta = await get_generation(generation_id, username)
    if not meta:
        raise ValueError("生成任务不存在或无权访问")
    if meta.get("status") != GENERATION_STATUS_PAUSED:
        raise ValueError("当前任务不在待确认状态")

    checkpoint = await get_generation_checkpoint(generation_id)
    if not checkpoint:
        raise ValueError("检查点不存在或已过期")
    if str(checkpoint.get("action_id") or "") != str(action_id):
        raise ValueError("action_id 不匹配")

    if checkpoint.get("user_response"):
        raise ValueError("该确认已处理，请勿重复提交")

    expires_at = checkpoint.get("expires_at")
    if expires_at and int(time.time()) > int(expires_at):
        if approved:
            raise ValueError("确认已超时，操作已自动取消")
        raise ValueError("确认已超时")

    await revoke_expire_task(generation_id, action_id)

    checkpoint["user_response"] = {
        "approved": approved,
        "draft": draft if isinstance(draft, dict) else checkpoint.get("draft"),
    }
    await save_generation_checkpoint(generation_id, checkpoint)

    await update_generation_meta(
        generation_id,
        {"status": GENERATION_STATUS_RUNNING},
    )
    celery_task_id = await schedule_continue_task(generation_id)

    offset = int(meta.get("offset") or 0)
    return {
        "generation_id": generation_id,
        "message_id": meta.get("message_id"),
        "offset": offset,
        "status": GENERATION_STATUS_RUNNING,
        "approved": approved,
        "celery_task_id": celery_task_id,
        "stream_path": f"/api/chat/generations/{generation_id}/stream",
    }


async def generation_stream_tail_closed(generation_id: str) -> bool:
    """Redis chunks 末尾是否已是 SSE [DONE]。"""
    length = await get_chunks_len(generation_id)
    if length <= 0:
        return False
    tail = await get_chunks_range(generation_id, length - 1, length - 1)
    if not tail:
        return False
    return "[DONE]" in tail[0]


async def abort_generation_as_cancelled(
    generation_id: str,
    *,
    append_tail: bool = True,
) -> None:
    """停止生成并收尾：保留已输出内容，标记 cancelled。"""
    from chat.generation_cancel import clear_generation_cancel_state

    meta = await get_generation_meta(generation_id)
    if not meta:
        return

    status = meta.get("status") or ""
    if status in (
        GENERATION_STATUS_COMPLETED,
        GENERATION_STATUS_FAILED,
        GENERATION_STATUS_CANCELLED,
    ):
        await clear_generation_cancel_state(generation_id)
        return

    buffer = get_buffer(generation_id)
    chunk_id = str(
        (buffer.chunk_id if buffer else None) or meta.get("chunk_id") or ""
    )

    workflow_data = meta.get("workflow_data")
    if not isinstance(workflow_data, dict):
        workflow_data = {}
    workflow_data = dict(workflow_data)
    workflow_data["status"] = "cancelled"

    content = str(meta.get("content") or "")
    reasoning = str(meta.get("reasoning_content") or "")
    blocks: list[dict[str, Any]] | None = None
    raw_blocks = meta.get("blocks")
    if isinstance(raw_blocks, list):
        blocks = [item for item in raw_blocks if isinstance(item, dict)]

    if buffer:
        content = str(buffer.content or content)
        reasoning = str(buffer.reasoning_content or reasoning)
        if buffer.blocks:
            blocks = list(buffer.blocks)

    content, reasoning, blocks, used_fallback = _resolve_cancelled_output(
        content=content,
        reasoning=reasoning,
        blocks=blocks,
    )

    if append_tail and chunk_id:
        stop_line = format_chunk({}, finish_reason="stop", chunk_id=chunk_id)
        done_line = format_stream_done()
        sse_lines: list[str] = []
        if used_fallback:
            sse_lines.append(
                format_chunk(
                    {"role": "assistant", "content": content},
                    chunk_id=chunk_id,
                )
            )
        sse_lines.extend([stop_line, done_line])

        if buffer:
            for idx, line in enumerate(sse_lines):
                await buffer.append(
                    line,
                    content_delta=content if used_fallback and idx == 0 else "",
                    update_blocks=False,
                )
            if used_fallback:
                buffer.content = content
                buffer.blocks = blocks
            await flush_buffer(generation_id, force=True)
        else:
            offset = await get_chunks_len(generation_id)
            await append_chunks_batch(
                generation_id,
                sse_lines,
                content=content,
                reasoning_content=reasoning if reasoning else None,
                blocks=blocks,
                offset=offset + len(sse_lines),
            )

    await finalize_generation(
        generation_id,
        status=GENERATION_STATUS_CANCELLED,
        mode=meta.get("mode"),
        intent=meta.get("intent"),
        workflow_data=workflow_data,
        steps=meta.get("steps") if isinstance(meta.get("steps"), list) else [],
        content=content,
        reasoning_content=reasoning or None,
        blocks=blocks,
    )
    await delete_generation_payload(generation_id)
    await delete_generation_checkpoint(generation_id)
    await clear_generation_cancel_state(generation_id)


async def cancel_generation(
    generation_id: str,
    creator: str,
) -> dict[str, Any]:
    """用户主动停止生成（running / paused）。"""
    username = _normalize_username(creator)
    meta = await get_generation(generation_id, username)
    if not meta:
        raise ValueError("生成任务不存在或无权访问")

    status = meta.get("status") or GENERATION_STATUS_RUNNING
    terminal_statuses = (
        GENERATION_STATUS_COMPLETED,
        GENERATION_STATUS_FAILED,
        GENERATION_STATUS_CANCELLED,
    )
    if status in terminal_statuses:
        return {
            "generation_id": generation_id,
            "message_id": meta.get("message_id"),
            "offset": int(meta.get("offset") or 0),
            "status": status,
            "already_finished": True,
            "stream_path": f"/api/chat/generations/{generation_id}/stream",
        }

    from chat.generation_cancel import request_generation_cancel

    await request_generation_cancel(generation_id)

    celery_task_id = meta.get("celery_task_id")
    await revoke_generation_tasks(generation_id, celery_task_id)

    if status == GENERATION_STATUS_PAUSED:
        checkpoint = await get_generation_checkpoint(generation_id)
        if checkpoint and checkpoint.get("action_id"):
            await revoke_expire_task(generation_id, str(checkpoint["action_id"]))
        tail_closed = await generation_stream_tail_closed(generation_id)
        await abort_generation_as_cancelled(
            generation_id,
            append_tail=not tail_closed,
        )
    else:
        if get_buffer(generation_id):
            tail_closed = await generation_stream_tail_closed(generation_id)
            await abort_generation_as_cancelled(
                generation_id,
                append_tail=not tail_closed,
            )
        else:
            await notify_generation_signal(generation_id)

    meta_after = await get_generation_meta(generation_id) or meta
    return {
        "generation_id": generation_id,
        "message_id": meta_after.get("message_id"),
        "offset": int(meta_after.get("offset") or 0),
        "status": meta_after.get("status") or GENERATION_STATUS_CANCELLED,
        "already_finished": False,
        "stream_path": f"/api/chat/generations/{generation_id}/stream",
    }


async def load_generation_buffer_blocks(
    generation_id: str,
    meta: dict[str, Any],
) -> list[dict[str, Any]]:
    """从 Redis meta 或 session 消息恢复 blocks。"""
    raw_blocks = meta.get("blocks")
    if isinstance(raw_blocks, list) and raw_blocks:
        return [item for item in raw_blocks if isinstance(item, dict)]

    session_id = meta.get("session_id")
    message_id = meta.get("message_id")
    if not session_id or not message_id:
        return []

    session = await ChatAgentSessionModel.filter(session_id=session_id).first()
    if not session:
        return []

    messages = session.messages if isinstance(session.messages, list) else []
    for item in messages:
        if not isinstance(item, dict):
            continue
        item_metadata = item.get("metadata") or {}
        if str(item_metadata.get("id") or "") != str(message_id):
            continue
        return load_blocks_from_message_metadata(item_metadata)
    return []


async def sync_session_assistant_from_generation(
    generation_id: str,
    *,
    final: bool = False,
) -> None:
    """将生成结果写入 session.messages；仅在 finalize / pause / cancel 时调用，流式中间态只存 Redis。"""
    buffer = get_buffer(generation_id)
    if buffer:
        meta = _meta_from_buffer(buffer)
    else:
        meta = await get_generation_meta(generation_id)
    if not meta:
        return

    session_id = meta.get("session_id")
    message_id = meta.get("message_id")
    if not session_id or not message_id:
        return

    session = await ChatAgentSessionModel.filter(session_id=session_id).first()
    if not session or session.status == CHAT_SESSION_STATUS_DELETED:
        return

    messages = session.messages if isinstance(session.messages, list) else []
    updated = False
    for item in messages:
        if not isinstance(item, dict):
            continue
        item_metadata = item.get("metadata") or {}
        if str(item_metadata.get("id") or "") != message_id:
            continue

        item["content"] = meta.get("content") or flatten_blocks(meta.get("blocks") or [])
        reasoning = str(meta.get("reasoning_content") or "")
        blocks = meta.get("blocks")
        if not reasoning and isinstance(blocks, list):
            reasoning = flatten_reasoning_blocks(blocks)
        if reasoning:
            item["reasoning_content"] = reasoning

        item_metadata = dict(item_metadata)
        if isinstance(blocks, list):
            item_metadata["blocks"] = blocks
        status = meta.get("status") or GENERATION_STATUS_RUNNING
        if final:
            if status == GENERATION_STATUS_COMPLETED:
                item_metadata["status"] = "completed"
            elif status == GENERATION_STATUS_CANCELLED:
                item_metadata["status"] = "cancelled"
            else:
                item_metadata["status"] = status
            item_metadata["mode"] = meta.get("mode")
            item_metadata["intent"] = meta.get("intent")
            workflow_data = meta.get("workflow_data")
            if isinstance(workflow_data, dict) and workflow_data:
                item_metadata["workflow_data"] = workflow_data
            completed_at = str(meta.get("completed_at") or "").strip()
            if not completed_at and status in (
                GENERATION_STATUS_COMPLETED,
                GENERATION_STATUS_CANCELLED,
            ):
                completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if completed_at:
                item_metadata["completed_at"] = completed_at
                item["created_at"] = completed_at
            thinking_ms = _as_int_or_none(meta.get("thinking_ms"))
            if thinking_ms is not None:
                item_metadata["thinking_ms"] = thinking_ms
            duration_ms = _as_int_or_none(meta.get("duration_ms"))
            if duration_ms is not None:
                item_metadata["duration_ms"] = duration_ms
            item_metadata.pop("generation_id", None)
        else:
            if status == GENERATION_STATUS_PAUSED:
                item_metadata["status"] = "paused"
                workflow_data = meta.get("workflow_data")
                if isinstance(workflow_data, dict) and workflow_data:
                    item_metadata["workflow_data"] = workflow_data
            else:
                item_metadata["status"] = "streaming"
            item_metadata["generation_id"] = generation_id

        item["metadata"] = item_metadata
        updated = True
        break

    if updated:
        await ChatAgentSessionModel.filter(id=session.id).update(messages=messages)


async def _replay_chunks(
    generation_id: str,
    from_offset: int,
) -> tuple[list[str], int, GenerationBuffer | None]:
    """阶段一：回放历史 chunk，返回 (lines, snapshot_len, buffer)。"""
    buffer = get_buffer(generation_id)
    from_offset = max(from_offset, 0)

    if buffer:
        async with buffer.lock:
            snapshot_len = len(buffer.chunks)
            replay = list(buffer.chunks[from_offset:snapshot_len])
        return replay, snapshot_len, buffer

    redis_len = await get_chunks_len(generation_id)
    if from_offset < redis_len:
        replay = await get_chunks_range(generation_id, from_offset, redis_len - 1)
    else:
        replay = []
    return replay, redis_len, None


def _get_action_expires_at(
    meta: dict[str, Any],
    checkpoint: dict[str, Any] | None,
) -> int | None:
    expires_at = checkpoint.get("expires_at") if checkpoint else None
    if expires_at is not None:
        return int(expires_at)
    workflow_data = meta.get("workflow_data")
    if isinstance(workflow_data, dict) and workflow_data.get("expires_at") is not None:
        return int(workflow_data["expires_at"])
    return None


def _compute_pause_continuation_wait_sec(
    meta: dict[str, Any],
    checkpoint: dict[str, Any] | None,
) -> float:
    """paused 且无新 chunk 时，判断是否应等待续跑 worker（超时/已 respond）。"""
    settings = get_chat_redis_settings()
    grace = float(settings.get("action_confirm_stream_grace_sec") or 30)
    worker_wait = 120.0
    pre_expire_window = 10.0

    if checkpoint and checkpoint.get("user_response"):
        return worker_wait

    expires_at = _get_action_expires_at(meta, checkpoint)
    if expires_at is None:
        return 0.0

    now = time.time()
    if now >= expires_at:
        return grace + worker_wait

    if now >= expires_at - pre_expire_window:
        return (expires_at - now) + grace + worker_wait

    return 0.0


async def _stream_live_from_redis(
    generation_id: str,
    snapshot_len: int,
    *,
    allow_paused: bool = False,
    max_wait_sec: float | None = None,
) -> AsyncIterator[str]:
    settings = get_chat_redis_settings()
    signal_timeout = float(settings["signal_timeout_sec"])
    seen_len = snapshot_len
    deadline = (time.time() + max_wait_sec) if max_wait_sec else None

    while True:
        if deadline is not None and time.time() >= deadline:
            break

        meta = await get_generation_meta(generation_id)
        if not meta:
            break
        redis_len = await get_chunks_len(generation_id)
        while seen_len < redis_len:
            lines = await get_chunks_range(generation_id, seen_len, redis_len - 1)
            for line in lines:
                yield line
            seen_len = redis_len

        status = meta.get("status")
        if status == GENERATION_STATUS_RUNNING:
            wait_sec = signal_timeout
            if deadline is not None:
                wait_sec = min(wait_sec, max(deadline - time.time(), 0.05))
            if wait_sec <= 0:
                break
            await wait_signal(generation_id, wait_sec)
        elif status == GENERATION_STATUS_PAUSED and allow_paused:
            wait_sec = signal_timeout
            if deadline is not None:
                wait_sec = min(wait_sec, max(deadline - time.time(), 0.05))
            if wait_sec <= 0:
                break
            await wait_signal(generation_id, wait_sec)
        else:
            break


async def stream_generation_chunks(
    generation_id: str,
    creator: str,
    *,
    from_offset: int = 0,
) -> AsyncIterator[str]:
    """双阶段续传：回放历史 → live_start → 实时流。"""
    username = _normalize_username(creator)
    meta = await get_generation(generation_id, username)
    if not meta:
        raise ValueError("生成任务不存在或无权访问")

    from_offset = max(from_offset, 0)
    replay, snapshot_len, buffer = await _replay_chunks(generation_id, from_offset)

    for line in replay:
        yield line

    status = meta.get("status") or GENERATION_STATUS_RUNNING
    chunk_id = meta.get("chunk_id") or "1"

    if status == GENERATION_STATUS_PAUSED:
        if replay:
            return
        checkpoint = await get_generation_checkpoint(generation_id)
        wait_sec = _compute_pause_continuation_wait_sec(meta, checkpoint)
        if wait_sec <= 0:
            return
        from chat.stream_format import format_live_start_chunk

        yield format_live_start_chunk(snapshot_len, chunk_id=chunk_id)
        async for line in _stream_live_from_redis(
            generation_id,
            snapshot_len,
            allow_paused=True,
            max_wait_sec=wait_sec,
        ):
            yield line
        return

    if status != GENERATION_STATUS_RUNNING:
        tail, tail_len, _ = await _replay_chunks(generation_id, snapshot_len)
        for line in tail:
            yield line
        snapshot_len = tail_len

        if status == GENERATION_STATUS_FAILED and not replay and not tail:
            err = meta.get("error") or "生成失败"
            from chat.stream_format import format_chunk, format_stream_done

            yield format_chunk(
                {"role": "assistant", "content": err},
                chunk_id=chunk_id,
            )
            yield format_chunk({}, finish_reason="stop", chunk_id=chunk_id)
            yield format_stream_done()
        return

    from chat.stream_format import format_live_start_chunk

    yield format_live_start_chunk(snapshot_len, chunk_id=chunk_id)

    async for line in _stream_live_from_redis(generation_id, snapshot_len):
        yield line


async def schedule_continue_task(generation_id: str) -> str | None:
    from async_task_module.dispatch import kiq_task
    from async_task_module.tasks.chat_tasks import chat_continue_generation_task

    task_id = f"chat-gen-continue-{generation_id}"
    try:
        celery_id = (await kiq_task(
            chat_continue_generation_task,
            generation_id,
            task_id=task_id,
        )).task_id
        logger.info(
            f"已投递续跑 generation_id={generation_id} celery_task_id={celery_id}"
        )
        return celery_id
    except Exception as exc:
        logger.error(
            f"投递续跑失败 generation_id={generation_id}: {exc}",
            exc_info=True,
        )
        return None


def _expire_task_id(generation_id: str, action_id: str) -> str:
    return f"chat-gen-expire-{generation_id}-{action_id}"


async def schedule_expire_task(
    generation_id: str,
    action_id: str,
    *,
    countdown: int,
) -> str | None:
    from async_task_module.dispatch import kiq_task
    from async_task_module.tasks.chat_tasks import chat_expire_generation_action_task

    if countdown < 0:
        countdown = 0
    task_id = _expire_task_id(generation_id, action_id)
    try:
        celery_id = (await kiq_task(
            chat_expire_generation_action_task,
            generation_id,
            action_id,
            task_id=task_id,
            delay=countdown,
        )).task_id
        logger.info(
            f"已投递确认超时任务 generation_id={generation_id} action_id={action_id} "
            f"countdown={countdown} celery_task_id={celery_id}"
        )
        return celery_id
    except Exception as exc:
        logger.error(
            f"投递确认超时任务失败 generation_id={generation_id} action_id={action_id}: {exc}",
            exc_info=True,
        )
        return None


async def revoke_expire_task(generation_id: str, action_id: str) -> None:
    from async_task_module.dispatch import revoke_task

    task_id = _expire_task_id(generation_id, action_id)
    try:
        await revoke_task(task_id)
        logger.info(
            f"已撤销确认超时任务 generation_id={generation_id} action_id={action_id} task_id={task_id}"
        )
    except Exception as exc:
        logger.warning(
            f"撤销确认超时任务失败 generation_id={generation_id} action_id={action_id}: {exc}"
        )


async def revoke_generation_tasks(
    generation_id: str,
    celery_task_id: str | None = None,
    *,
    terminate: bool = False,
) -> None:
    """撤销 run/continue 任务（协作取消，terminate 参数保留兼容）。"""
    from async_task_module.dispatch import revoke_tasks

    task_ids = {
        f"chat-gen-{generation_id}",
        f"chat-gen-continue-{generation_id}",
    }
    if celery_task_id:
        task_ids.add(str(celery_task_id))

    try:
        await revoke_tasks(task_ids)
        for task_id in task_ids:
            logger.info(
                f"已撤销生成任务 generation_id={generation_id} task_id={task_id} terminate={terminate}"
            )
    except Exception as exc:
        logger.warning(
            f"撤销生成任务失败 generation_id={generation_id}: {exc}"
        )


async def schedule_generation_task(generation_id: str) -> str | None:
    from async_task_module.dispatch import kiq_task
    from async_task_module.tasks.chat_tasks import chat_run_generation_task

    task_id = f"chat-gen-{generation_id}"
    try:
        celery_id = (await kiq_task(
            chat_run_generation_task,
            generation_id,
            task_id=task_id,
        )).task_id
        logger.info(
            f"已投递生成任务 generation_id={generation_id} celery_task_id={celery_id}"
        )
        return celery_id
    except Exception as exc:
        logger.error(
            f"投递生成任务失败 generation_id={generation_id}: {exc}",
            exc_info=True,
        )
        return None


async def schedule_session_memory_task(session_id: str) -> str | None:
    from async_task_module.dispatch import kiq_task
    from async_task_module.tasks.chat_tasks import chat_update_session_memory_task

    task_id = f"chat-mem-{session_id}"
    try:
        celery_id = (await kiq_task(
            chat_update_session_memory_task,
            session_id,
            task_id=task_id,
        )).task_id
        logger.info(
            f"已投递会话摘要任务 session_id={session_id} celery_task_id={celery_id}"
        )
        return celery_id
    except Exception as exc:
        logger.error(
            f"投递会话摘要任务失败 session_id={session_id}: {exc}",
            exc_info=True,
        )
        return None
