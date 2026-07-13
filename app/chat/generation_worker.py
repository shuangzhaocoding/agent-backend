# -*- coding: utf-8 -*-
#
# Taskiq Worker 内执行 Agent 生成（读 Redis payload，写 Buffer + Redis）
#
from __future__ import annotations

import time
from typing import Any

from common.logger import logger

from chat.agent import (
    iter_agent_sse_events,
    iter_agent_sse_events_after_cancel,
    iter_agent_sse_events_after_confirm,
)
from chat.action_confirm import (
    execute_confirmed_action,
    format_cancel_text,
    format_timeout_text,
)
from chat.generation_buffer import GenerationBuffer, get_buffer, register_buffer
from chat.generation_cancel import (
    inject_generation_id_into_payload_context,
    is_generation_cancelled,
)
from chat.generation_service import (
    abort_generation_as_cancelled,
    append_generation_chunk,
    apply_generation_block_ops,
    finalize_generation,
    generation_stream_tail_closed,
    load_generation_buffer_blocks,
    pause_generation,
    parse_sse_delta,
)
from chat.message_blocks import sync_derived_from_blocks
from chat.redis_generation import (
    GENERATION_STATUS_CANCELLED,
    GENERATION_STATUS_COMPLETED,
    GENERATION_STATUS_FAILED,
    delete_generation_checkpoint,
    delete_generation_payload,
    get_chunks_range,
    get_generation_checkpoint,
    get_generation_meta,
    get_generation_payload,
)
from chat.schemas import AgentChatRequest
from chat.stream_format import format_chunk, format_stream_done


async def _init_worker_buffer(generation_id: str, meta: dict[str, Any]) -> GenerationBuffer:
    existing_chunks = await get_chunks_range(generation_id, 0, -1)
    buffer = GenerationBuffer(
        generation_id,
        session_id=str(meta.get("session_id") or ""),
        message_id=str(meta.get("message_id") or ""),
        chunk_id=str(meta.get("chunk_id") or ""),
        creator=str(meta.get("creator") or ""),
    )
    buffer.chunks = list(existing_chunks)
    buffer.blocks = await load_generation_buffer_blocks(generation_id, meta)
    if buffer.blocks:
        buffer.content, buffer.reasoning_content = sync_derived_from_blocks(buffer.blocks)
    else:
        buffer.content = str(meta.get("content") or "")
        buffer.reasoning_content = str(meta.get("reasoning_content") or "")
    register_buffer(buffer)
    return buffer


def _event_error_sse(chunk_id: str, message: str) -> str:
    return format_chunk(
        {"role": "assistant", "content": message},
        chunk_id=chunk_id,
    )


async def _finish_if_cancelled(generation_id: str) -> bool:
    if not await is_generation_cancelled(generation_id):
        return False
    tail_closed = await generation_stream_tail_closed(generation_id)
    await abort_generation_as_cancelled(generation_id, append_tail=not tail_closed)
    return True


async def _consume_agent_sse_events(
    generation_id: str,
    event_iter: Any,
    *,
    chunk_id: str,
) -> tuple[dict[str, Any] | None, bool]:
    final_data: dict[str, Any] | None = None
    cancelled = False

    async for event in event_iter:
        if await is_generation_cancelled(generation_id):
            cancelled = True
            break

        event_type = event.get("type")
        if event_type == "sse":
            sse_line = event["data"]["line"]
            delta = parse_sse_delta(sse_line)
            content_delta = delta.get("content") or ""
            reasoning_delta = delta.get("reasoning_content") or ""
            update_blocks = event["data"].get("update_blocks", True)
            await append_generation_chunk(
                generation_id,
                sse_line,
                content_delta=content_delta if isinstance(content_delta, str) else "",
                reasoning_delta=reasoning_delta if isinstance(reasoning_delta, str) else "",
                update_blocks=bool(update_blocks),
            )

        elif event_type == "block":
            await apply_generation_block_ops(
                generation_id,
                event.get("data", {}).get("ops") or [],
            )

        elif event_type == "done":
            final_data = event.get("data") or {}
            block_ops = final_data.get("block_ops")
            if block_ops:
                await apply_generation_block_ops(generation_id, block_ops)

    return final_data, cancelled


async def run_generation_async(generation_id: str, celery_task_id: str | None = None) -> None:
    meta = await get_generation_meta(generation_id)
    if not meta:
        logger.error(f"generation meta 不存在 generation_id={generation_id}")
        return

    if meta.get("status") == GENERATION_STATUS_CANCELLED:
        return

    if await _finish_if_cancelled(generation_id):
        return

    payload_data = await get_generation_payload(generation_id)
    if not payload_data:
        logger.error(f"generation payload 不存在 generation_id={generation_id}")
        return

    payload_data = inject_generation_id_into_payload_context(payload_data, generation_id)

    try:
        payload = AgentChatRequest.model_validate(payload_data)
    except Exception as exc:
        logger.error(f"payload 解析失败 generation_id={generation_id}: {exc}")
        return

    buffer = await _init_worker_buffer(generation_id, meta)
    chunk_id = buffer.chunk_id

    if celery_task_id:
        from chat.redis_generation import update_generation_meta

        await update_generation_meta(
            generation_id,
            {"celery_task_id": celery_task_id},
        )

    try:
        final_data, cancelled = await _consume_agent_sse_events(
            generation_id,
            iter_agent_sse_events(payload, chunk_id=chunk_id),
            chunk_id=chunk_id,
        )
        if cancelled:
            tail_closed = await generation_stream_tail_closed(generation_id)
            await abort_generation_as_cancelled(
                generation_id,
                append_tail=not tail_closed,
            )
            return

        if not final_data:
            if await is_generation_cancelled(generation_id):
                tail_closed = await generation_stream_tail_closed(generation_id)
                await abort_generation_as_cancelled(
                    generation_id,
                    append_tail=not tail_closed,
                )
            return

        if final_data.get("paused"):
            buf = get_buffer(generation_id)
            await pause_generation(
                generation_id,
                checkpoint=final_data.get("checkpoint") or {},
                content=buf.content if buf else (final_data.get("content") or ""),
                reasoning_content=final_data.get("reasoning_content"),
                mode=final_data.get("mode"),
                intent=final_data.get("intent"),
                workflow_data=final_data.get("workflow_data"),
                steps=final_data.get("steps"),
                blocks=list(buf.blocks) if buf else None,
                thinking_ms=final_data.get("thinking_ms"),
                duration_ms=final_data.get("duration_ms"),
            )
            return

        buf = get_buffer(generation_id)
        await finalize_generation(
            generation_id,
            status=GENERATION_STATUS_COMPLETED,
            mode=final_data.get("mode"),
            intent=final_data.get("intent"),
            workflow_data=final_data.get("workflow_data") or {},
            steps=final_data.get("steps") or [],
            content=buf.content if buf else (final_data.get("content") or ""),
            reasoning_content=final_data.get("reasoning_content") or None,
            blocks=list(buf.blocks) if buf else None,
            thinking_ms=final_data.get("thinking_ms"),
            duration_ms=final_data.get("duration_ms"),
        )
    except Exception as exc:
        logger.error(f"Celery 生成失败 generation_id={generation_id}", exc_info=True)
        err_sse = _event_error_sse(chunk_id, str(exc))
        await append_generation_chunk(generation_id, err_sse, content_delta=str(exc))
        await append_generation_chunk(
            generation_id,
            format_chunk({}, finish_reason="stop", chunk_id=chunk_id),
        )
        await append_generation_chunk(generation_id, format_stream_done())
        await finalize_generation(
            generation_id,
            status=GENERATION_STATUS_FAILED,
            error=str(exc),
            content=str(exc),
        )
    finally:
        meta_after = await get_generation_meta(generation_id)
        status = (meta_after or {}).get("status")
        if status in (GENERATION_STATUS_COMPLETED, GENERATION_STATUS_FAILED, GENERATION_STATUS_CANCELLED):
            await delete_generation_payload(generation_id)
            await delete_generation_checkpoint(generation_id)


async def continue_generation_async(generation_id: str, celery_task_id: str | None = None) -> None:
    meta = await get_generation_meta(generation_id)
    checkpoint = await get_generation_checkpoint(generation_id)
    if not meta or not checkpoint:
        logger.error(f"续跑缺少 meta/checkpoint generation_id={generation_id}")
        return

    if meta.get("status") == GENERATION_STATUS_CANCELLED:
        return

    if await _finish_if_cancelled(generation_id):
        return

    payload_data = await get_generation_payload(generation_id)
    if not payload_data:
        logger.error(f"续跑 payload 不存在 generation_id={generation_id}")
        return

    payload_data = inject_generation_id_into_payload_context(payload_data, generation_id)

    try:
        payload = AgentChatRequest.model_validate(payload_data)
    except Exception as exc:
        logger.error(f"续跑 payload 解析失败 generation_id={generation_id}: {exc}")
        return

    buffer = await _init_worker_buffer(generation_id, meta)
    chunk_id = buffer.chunk_id
    user_response = checkpoint.get("user_response") or {}
    approved = bool(user_response.get("approved"))
    draft = user_response.get("draft") or checkpoint.get("draft") or {}

    if celery_task_id:
        from chat.redis_generation import update_generation_meta

        await update_generation_meta(generation_id, {"celery_task_id": celery_task_id})

    try:
        if not approved:
            action_type = str(checkpoint.get("action_type") or "")
            if user_response.get("reason") == "timeout":
                cancel_text = format_timeout_text(action_type)
            else:
                cancel_text = format_cancel_text(action_type)
            final_data, cancelled = await _consume_agent_sse_events(
                generation_id,
                iter_agent_sse_events_after_cancel(
                    checkpoint,
                    cancel_text,
                    chunk_id=chunk_id,
                    payload=payload,
                ),
                chunk_id=chunk_id,
            )
            if cancelled:
                tail_closed = await generation_stream_tail_closed(generation_id)
                await abort_generation_as_cancelled(
                    generation_id,
                    append_tail=not tail_closed,
                )
                return

            if final_data and final_data.get("paused"):
                buf = get_buffer(generation_id)
                await pause_generation(
                    generation_id,
                    checkpoint=final_data.get("checkpoint") or {},
                    content=buf.content if buf else (final_data.get("content") or ""),
                    reasoning_content=final_data.get("reasoning_content"),
                    mode=final_data.get("mode"),
                    intent=final_data.get("intent"),
                    workflow_data=final_data.get("workflow_data"),
                    steps=final_data.get("steps"),
                    blocks=list(buf.blocks) if buf else None,
                    thinking_ms=final_data.get("thinking_ms"),
                    duration_ms=final_data.get("duration_ms"),
                )
                return
            buf = get_buffer(generation_id)
            await finalize_generation(
                generation_id,
                status=GENERATION_STATUS_CANCELLED,
                mode=checkpoint.get("mode"),
                intent=checkpoint.get("intent"),
                workflow_data=(final_data or {}).get("workflow_data") or {},
                steps=(final_data or {}).get("steps") or checkpoint.get("steps") or [],
                content=buf.content if buf else ((final_data or {}).get("content") or cancel_text),
                blocks=list(buf.blocks) if buf else None,
                thinking_ms=(final_data or {}).get("thinking_ms"),
                duration_ms=(final_data or {}).get("duration_ms"),
            )
            return

        context = dict(payload.context or {})
        action_result = await execute_confirmed_action(
            str(checkpoint.get("action_type") or ""),
            draft if isinstance(draft, dict) else {},
            user_email=checkpoint.get("user_email") or context.get("user_email"),
            context=context,
        )

        if await is_generation_cancelled(generation_id):
            tail_closed = await generation_stream_tail_closed(generation_id)
            await abort_generation_as_cancelled(
                generation_id,
                append_tail=not tail_closed,
            )
            return

        final_data, cancelled = await _consume_agent_sse_events(
            generation_id,
            iter_agent_sse_events_after_confirm(
                payload,
                checkpoint,
                action_result,
                chunk_id=chunk_id,
            ),
            chunk_id=chunk_id,
        )

        if cancelled:
            tail_closed = await generation_stream_tail_closed(generation_id)
            await abort_generation_as_cancelled(
                generation_id,
                append_tail=not tail_closed,
            )
            return

        if final_data and final_data.get("paused"):
            buf = get_buffer(generation_id)
            await pause_generation(
                generation_id,
                checkpoint=final_data.get("checkpoint") or {},
                content=buf.content if buf else (final_data.get("content") or ""),
                reasoning_content=final_data.get("reasoning_content"),
                mode=final_data.get("mode"),
                intent=final_data.get("intent"),
                workflow_data=final_data.get("workflow_data"),
                steps=final_data.get("steps"),
                blocks=list(buf.blocks) if buf else None,
                thinking_ms=final_data.get("thinking_ms"),
                duration_ms=final_data.get("duration_ms"),
            )
            return

        buf = get_buffer(generation_id)
        await finalize_generation(
            generation_id,
            status=GENERATION_STATUS_COMPLETED,
            mode=(final_data or {}).get("mode") or checkpoint.get("mode"),
            intent=(final_data or {}).get("intent") or checkpoint.get("intent"),
            workflow_data=(final_data or {}).get("workflow_data") or {},
            steps=(final_data or {}).get("steps") or [],
            content=buf.content if buf else "",
            reasoning_content=buf.reasoning_content if buf else None,
            blocks=list(buf.blocks) if buf else None,
            thinking_ms=(final_data or {}).get("thinking_ms"),
            duration_ms=(final_data or {}).get("duration_ms"),
        )
    except Exception as exc:
        logger.error(f"Celery 续跑失败 generation_id={generation_id}", exc_info=True)
        err_sse = _event_error_sse(chunk_id, str(exc))
        await append_generation_chunk(generation_id, err_sse, content_delta=str(exc))
        await append_generation_chunk(
            generation_id,
            format_chunk({}, finish_reason="stop", chunk_id=chunk_id),
        )
        await append_generation_chunk(generation_id, format_stream_done())
        await finalize_generation(
            generation_id,
            status=GENERATION_STATUS_FAILED,
            error=str(exc),
        )
    finally:
        meta_after = await get_generation_meta(generation_id)
        status = (meta_after or {}).get("status")
        if status in (GENERATION_STATUS_COMPLETED, GENERATION_STATUS_FAILED, GENERATION_STATUS_CANCELLED):
            await delete_generation_payload(generation_id)
            await delete_generation_checkpoint(generation_id)


async def expire_generation_action_async(generation_id: str, action_id: str) -> None:
    """确认超时后自动按取消续跑。"""
    from chat.redis_generation import (
        GENERATION_STATUS_PAUSED,
        GENERATION_STATUS_RUNNING,
        save_generation_checkpoint,
        update_generation_meta,
    )

    meta = await get_generation_meta(generation_id)
    if not meta or meta.get("status") != GENERATION_STATUS_PAUSED:
        return

    if await is_generation_cancelled(generation_id):
        tail_closed = await generation_stream_tail_closed(generation_id)
        await abort_generation_as_cancelled(generation_id, append_tail=not tail_closed)
        return

    checkpoint = await get_generation_checkpoint(generation_id)
    if not checkpoint:
        return
    if str(checkpoint.get("action_id") or "") != str(action_id):
        return
    if checkpoint.get("user_response"):
        return

    expires_at = checkpoint.get("expires_at")
    if expires_at and int(time.time()) < int(expires_at):
        return

    checkpoint["user_response"] = {
        "approved": False,
        "draft": checkpoint.get("draft"),
        "reason": "timeout",
    }
    await save_generation_checkpoint(generation_id, checkpoint)
    await update_generation_meta(generation_id, {"status": GENERATION_STATUS_RUNNING})

    from chat.generation_service import schedule_continue_task

    await schedule_continue_task(generation_id)
    logger.info(
        f"确认已超时，自动取消续跑 generation_id={generation_id} action_id={action_id}"
    )
