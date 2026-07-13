# -*- coding: utf-8 -*-
#
# Agent 编排：ReAct 多轮推理 + OpenAI 兼容流式 / JSON 非流式
#
import json
import time
import uuid
from typing import Any, AsyncIterator

from common.logger import logger

from chat.action_confirm import (
    ACTION_TYPE_CREATE_JIRA,
    build_checkpoint,
    format_action_result_text,
)
from chat.locale import (
    format_thinking_max_steps,
    format_thinking_skill_routing,
    format_thinking_skills_selected,
    format_thinking_tool_done,
    format_thinking_tool_start,
    get_locale_from_context,
)
from chat.orchestrator import run_agent_orchestrator
from chat.react import (
    ReactStepRecord,
    append_tool_message,
    continue_react_loop,
    process_after_pending_tool_cancelled,
    process_remaining_tool_calls_after_confirm,
    steps_from_dicts,
    stream_final_answer,
    stream_plain_text,
)
from chat.schemas import AgentChatRequest
from chat.stream_format import (
    format_action_required_chunk,
    format_agent_metadata_chunk,
    format_chunk,
    format_stream_done,
    format_thinking_done_chunk,
)


async def iter_agent_events(payload: AgentChatRequest) -> AsyncIterator[dict[str, Any]]:
    """产出结构化事件，供非流式 JSON 响应复用。"""
    draft_answer: str | None = None
    steps: list[ReactStepRecord] = []
    mode = "react"
    intent: str | None = None
    workflow_data: dict[str, Any] = {}
    reasoning_content: str | None = None

    try:
        async for event in run_agent_orchestrator(payload):
            if event.type == "action_required":
                data = event.data
                workflow_data = _paused_workflow_data(data)
                steps = steps_from_dicts(data.get("steps") or [])
                mode = data.get("mode") or "react"
                intent = data.get("intent")
                yield {
                    "type": "done",
                    "data": {
                        "paused": True,
                        "content": data.get("preview_text") or "",
                        "mode": mode,
                        "intent": intent,
                        "workflow_data": workflow_data,
                        "checkpoint": _checkpoint_from_action_data(data),
                        "steps": [
                            {
                                "step": s.step,
                                "thought": s.thought,
                                "action": s.action,
                                "action_input": s.action_input,
                                "observation": s.observation,
                            }
                            for s in steps
                        ],
                    },
                }
                return
            if event.type == "finish":
                draft_answer = event.data.get("final_answer")
                steps = steps_from_dicts(event.data.get("steps") or [])
                mode = event.data.get("mode") or "react"
                intent = event.data.get("intent")
                workflow_data = event.data.get("workflow_data") or {}
                reasoning_content = event.data.get("reasoning_content")
            elif event.type == "max_steps":
                steps = steps_from_dicts(event.data.get("steps") or [])
    except Exception as exc:
        logger.error("ReAct 循环失败", exc_info=True)
        yield {"type": "error", "data": {"message": str(exc)}}
        return

    full_content = ""
    temperature = payload.temperature or 0.7

    try:
        draft = (draft_answer or "").strip()
        if draft:
            async for delta in stream_plain_text(draft):
                full_content += delta
        else:
            async for delta in stream_final_answer(
                payload,
                steps=steps,
                draft_answer=draft_answer,
                temperature=temperature,
            ):
                full_content += delta
    except Exception as exc:
            logger.error("最终回答生成失败", exc_info=True)
            yield {"type": "error", "data": {"message": str(exc)}}
            return

    yield {
        "type": "done",
        "data": {
            "content": full_content,
            "mode": mode,
            "intent": intent,
            "workflow_data": workflow_data,
            "thinking": payload.thinking,
            "reasoning_content": None,
            "steps": [
                {
                    "step": s.step,
                    "thought": s.thought,
                    "action": s.action,
                    "action_input": s.action_input,
                    "observation": s.observation,
                }
                for s in steps
            ],
        },
    }


def _format_reasoning_thought(data: dict[str, Any]) -> str:
    reasoning = data.get("reasoning_content") or data.get("thought") or ""
    action = data.get("action") or ""
    action_input = data.get("action_input") or {}
    lines = []
    if reasoning:
        lines.append(reasoning)
    if action:
        lines.append(f"Action: {action}")
        if action_input:
            lines.append(f"Action Input: {json.dumps(action_input, ensure_ascii=False)}")
    return "\n".join(lines) + ("\n" if lines else "")


def _paused_workflow_data(action_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "paused",
        "action_id": action_data.get("action_id"),
        "action_type": action_data.get("action_type"),
        "title": action_data.get("title"),
        "draft": action_data.get("draft"),
        "kind": action_data.get("kind"),
        "respond_api": action_data.get("respond_api"),
        "confirm_timeout_sec": action_data.get("confirm_timeout_sec"),
        "paused_at": action_data.get("paused_at"),
        "expires_at": action_data.get("expires_at"),
    }


def _checkpoint_from_action_data(action_data: dict[str, Any]) -> dict[str, Any]:
    extra = {
        k: action_data[k]
        for k in (
            "robot_info",
            "issues_count",
            "issue_index",
            "attachment_count",
            "files",
        )
        if k in action_data
    }
    return build_checkpoint(
        action_id=str(action_data.get("action_id") or ""),
        action_type=str(action_data.get("action_type") or ""),
        draft=action_data.get("draft") or {},
        kind=str(action_data.get("kind") or ""),
        preview_text=str(action_data.get("preview_text") or ""),
        mode=action_data.get("mode"),
        intent=action_data.get("intent"),
        steps=action_data.get("steps"),
        react_messages=action_data.get("react_messages"),
        pending_tool_call=action_data.get("pending_tool_call"),
        remaining_tool_calls=action_data.get("remaining_tool_calls"),
        step_idx=action_data.get("step_idx"),
        user_email=action_data.get("user_email"),
        confirm_timeout_sec=action_data.get("confirm_timeout_sec"),
        paused_at=action_data.get("paused_at"),
        expires_at=action_data.get("expires_at"),
        selected_skill_ids=action_data.get("selected_skill_ids"),
        direct_reply=action_data.get("direct_reply"),
        extra=extra or None,
    )


async def _yield_action_required_sse(
    action_data: dict[str, Any],
    *,
    chunk_id: str,
    created: int,
    thinking: bool,
    role_sent: bool,
    thinking_ms: int | None = None,
    duration_ms: int | None = None,
) -> AsyncIterator[dict[str, Any]]:
    for item in _iter_seal_reasoning_events(
        thinking_ms,
        chunk_id=chunk_id,
        created=created,
    ):
        yield item

    preview = (action_data.get("preview_text") or "").strip()
    mode = action_data.get("mode") or "react"
    intent = action_data.get("intent")
    steps = steps_from_dicts(action_data.get("steps") or [])
    workflow_data = _paused_workflow_data(action_data)
    full_content = ""
    full_reasoning = ""

    if preview:
        async for delta in stream_plain_text(preview):
            full_content += delta
            if not role_sent:
                line = format_chunk(
                    {"role": "assistant", "content": delta},
                    chunk_id=chunk_id,
                    created=created,
                )
                role_sent = True
            else:
                line = format_chunk(
                    {"content": delta},
                    chunk_id=chunk_id,
                    created=created,
                )
            yield {"type": "sse", "data": {"line": line}}

    line = format_action_required_chunk(action_data, chunk_id=chunk_id, created=created)
    yield {"type": "sse", "data": {"line": line}}

    line = format_agent_metadata_chunk(
        _build_stream_metadata(
            chunk_id=chunk_id,
            mode=str(mode),
            intent=intent,
            workflow_data=workflow_data,
            steps=steps,
            thinking_ms=thinking_ms,
            duration_ms=duration_ms,
        ),
        chunk_id=chunk_id,
        created=created,
    )
    yield {"type": "sse", "data": {"line": line}}
    yield {
        "type": "sse",
        "data": {"line": format_chunk({}, finish_reason="stop", chunk_id=chunk_id, created=created)},
    }
    yield {"type": "sse", "data": {"line": format_stream_done()}}

    yield {
        "type": "block",
        "data": {
            "ops": [
                {
                    "op": "append_action_card",
                    "action_data": action_data,
                    "status": "pending",
                }
            ]
        },
    }

    done_data: dict[str, Any] = {
        "paused": True,
        "checkpoint": _checkpoint_from_action_data(action_data),
        "content": full_content,
        "mode": mode,
        "intent": intent,
        "workflow_data": workflow_data,
        "reasoning_content": full_reasoning if thinking else None,
        "steps": [
            {
                "step": s.step,
                "thought": s.thought,
                "action": s.action,
                "action_input": s.action_input,
                "observation": s.observation,
            }
            for s in steps
        ],
    }
    done_data.update(_timing_fields(thinking_ms=thinking_ms, duration_ms=duration_ms))
    yield {"type": "done", "data": done_data}


def _format_message_time() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _timing_fields(
    *,
    thinking_ms: int | None = None,
    duration_ms: int | None = None,
) -> dict[str, int]:
    fields: dict[str, int] = {}
    if thinking_ms is not None:
        fields["thinking_ms"] = max(0, int(thinking_ms))
    if duration_ms is not None:
        fields["duration_ms"] = max(0, int(duration_ms))
    return fields


def _iter_seal_reasoning_events(
    thinking_ms: int | None,
    *,
    chunk_id: str,
    created: int,
) -> list[dict[str, Any]]:
    """本轮思考结束：写入 reasoning block，并推送 thinking_done SSE。"""
    if thinking_ms is None:
        return []
    ms = max(0, int(thinking_ms))
    return [
        {
            "type": "block",
            "data": {
                "ops": [
                    {
                        "op": "seal_reasoning",
                        "thinking_ms": ms,
                    }
                ]
            },
        },
        {
            "type": "sse",
            "data": {
                "line": format_thinking_done_chunk(
                    ms,
                    chunk_id=chunk_id,
                    created=created,
                ),
                "update_blocks": False,
            },
        },
    ]


def _build_stream_metadata(
    *,
    chunk_id: str,
    mode: str,
    intent: str | None,
    workflow_data: dict[str, Any],
    steps: list[ReactStepRecord],
    payload: AgentChatRequest | None = None,
    reasoning_content: str | None = None,
    include_completed_at: bool = False,
    thinking_ms: int | None = None,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    steps_data = [
        {
            "step": s.step,
            "thought": s.thought,
            "action": s.action,
            "action_input": s.action_input,
            "observation": s.observation,
        }
        for s in steps
    ]
    meta: dict[str, Any] = {
        "id": chunk_id,
        "mode": mode,
        "intent": intent,
        "workflow_data": workflow_data,
        "steps": steps_data,
    }
    if include_completed_at:
        meta["completed_at"] = _format_message_time()
    meta.update(_timing_fields(thinking_ms=thinking_ms, duration_ms=duration_ms))
    if payload is not None:
        from chat.context_usage import build_context_usage

        llm_mode = "react"
        meta["context_usage"] = build_context_usage(
            payload,
            mode=llm_mode,
            react_steps=steps_data,
            reasoning_content=reasoning_content if payload.thinking else None,
        )
    return meta


async def iter_agent_sse_events(
    payload: AgentChatRequest,
    *,
    chunk_id: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """
    产出 SSE 行与完成事件，供直连流式与后台生成任务复用。
    - type=sse: data.line 为完整 SSE data 行
    - type=done: 最终结构化结果
    """
    chunk_id = chunk_id or str(uuid.uuid4())
    created = int(time.time())
    started = time.perf_counter()
    role_sent = False
    thinking = payload.thinking
    locale = get_locale_from_context(payload.context)

    draft_answer: str | None = None
    steps: list[ReactStepRecord] = []
    mode = "react"
    intent: str | None = None
    workflow_data: dict[str, Any] = {}
    full_content = ""
    full_reasoning = ""
    thinking_ms = 0

    try:
        async for event in run_agent_orchestrator(payload):
            if event.type == "skill_routing":
                if thinking:
                    text = format_thinking_skill_routing(locale)
                    if text.strip():
                        line = format_chunk(
                            {"reasoning_content": text},
                            chunk_id=chunk_id,
                            created=created,
                        )
                        yield {"type": "sse", "data": {"line": line}}
                        full_reasoning += text
                continue
            if event.type == "skills_selected":
                if thinking:
                    text = format_thinking_skills_selected(event.data, locale)
                    if text.strip():
                        line = format_chunk(
                            {"reasoning_content": text},
                            chunk_id=chunk_id,
                            created=created,
                        )
                        yield {"type": "sse", "data": {"line": line}}
                        full_reasoning += text
                continue
            if event.type == "action_required":
                thinking_ms = _elapsed_ms(started)
                async for item in _yield_action_required_sse(
                    event.data,
                    chunk_id=chunk_id,
                    created=created,
                    thinking=thinking,
                    role_sent=role_sent,
                    thinking_ms=thinking_ms,
                    duration_ms=_elapsed_ms(started),
                ):
                    yield item
                return
            elif event.type == "thought" and thinking:
                text = _format_reasoning_thought(event.data)
                if text.strip():
                    line = format_chunk(
                        {"reasoning_content": text},
                        chunk_id=chunk_id,
                        created=created,
                    )
                    yield {"type": "sse", "data": {"line": line}}
                    full_reasoning += text
            elif event.type == "tool_start" and thinking:
                tool_name = event.data.get("tool_name") or ""
                text = format_thinking_tool_start(tool_name, locale)
                line = format_chunk(
                    {"reasoning_content": text},
                    chunk_id=chunk_id,
                    created=created,
                )
                yield {"type": "sse", "data": {"line": line}}
                full_reasoning += text
            elif event.type == "tool_done" and thinking:
                tool_name = event.data.get("tool_name") or ""
                text = format_thinking_tool_done(tool_name, locale)
                line = format_chunk(
                    {"reasoning_content": text},
                    chunk_id=chunk_id,
                    created=created,
                )
                yield {"type": "sse", "data": {"line": line}}
                full_reasoning += text
            elif event.type == "finish":
                draft_answer = event.data.get("final_answer")
                steps = steps_from_dicts(event.data.get("steps") or [])
                mode = event.data.get("mode") or "react"
                intent = event.data.get("intent")
                workflow_data = event.data.get("workflow_data") or {}
            elif event.type == "max_steps":
                if thinking:
                    text = event.data.get("message") or format_thinking_max_steps(
                        payload.max_steps or 6,
                        locale,
                    )
                    line = format_chunk(
                        {"reasoning_content": text},
                        chunk_id=chunk_id,
                        created=created,
                    )
                    yield {"type": "sse", "data": {"line": line}}
                    full_reasoning += text
                steps = steps_from_dicts(event.data.get("steps") or [])

        thinking_ms = _elapsed_ms(started)
        for item in _iter_seal_reasoning_events(
            thinking_ms,
            chunk_id=chunk_id,
            created=created,
        ):
            yield item
        temperature = payload.temperature or 0.7
        draft = (draft_answer or "").strip()
        if draft:
            delta_iter = stream_plain_text(draft)
        else:
            delta_iter = stream_final_answer(
                payload,
                steps=steps,
                draft_answer=draft_answer,
                temperature=temperature,
            )
        async for delta in delta_iter:
            if not role_sent:
                line = format_chunk(
                    {"role": "assistant", "content": delta},
                    chunk_id=chunk_id,
                    created=created,
                )
                role_sent = True
            else:
                line = format_chunk(
                    {"content": delta},
                    chunk_id=chunk_id,
                    created=created,
                )
            yield {"type": "sse", "data": {"line": line}}
            full_content += delta

        duration_ms = _elapsed_ms(started)
        line = format_agent_metadata_chunk(
            _build_stream_metadata(
                chunk_id=chunk_id,
                mode=mode,
                intent=intent,
                workflow_data=workflow_data,
                steps=steps,
                payload=payload,
                reasoning_content=full_reasoning if thinking else None,
                include_completed_at=True,
                thinking_ms=thinking_ms,
                duration_ms=duration_ms,
            ),
            chunk_id=chunk_id,
            created=created,
        )
        yield {"type": "sse", "data": {"line": line}}
        line = format_chunk({}, finish_reason="stop", chunk_id=chunk_id, created=created)
        yield {"type": "sse", "data": {"line": line}}
        yield {"type": "sse", "data": {"line": format_stream_done()}}

        done_data: dict[str, Any] = {
            "content": full_content,
            "mode": mode,
            "intent": intent,
            "workflow_data": workflow_data,
            "reasoning_content": full_reasoning if thinking else None,
            "steps": [
                {
                    "step": s.step,
                    "thought": s.thought,
                    "action": s.action,
                    "action_input": s.action_input,
                    "observation": s.observation,
                }
                for s in steps
            ],
        }
        done_data.update(_timing_fields(thinking_ms=thinking_ms, duration_ms=duration_ms))
        yield {"type": "done", "data": done_data}

    except Exception as exc:
        logger.error("Agent SSE 事件生成失败", exc_info=True)
        err_text = str(exc)
        if not role_sent:
            line = format_chunk(
                {"role": "assistant", "content": err_text},
                chunk_id=chunk_id,
                created=created,
            )
        else:
            line = format_chunk({"content": err_text}, chunk_id=chunk_id, created=created)
        yield {"type": "sse", "data": {"line": line}}
        line = format_chunk({}, finish_reason="stop", chunk_id=chunk_id, created=created)
        yield {"type": "sse", "data": {"line": line}}
        yield {"type": "sse", "data": {"line": format_stream_done()}}
        done_err: dict[str, Any] = {"content": err_text, "mode": mode, "workflow_data": {}}
        done_err.update(
            _timing_fields(
                thinking_ms=thinking_ms or _elapsed_ms(started),
                duration_ms=_elapsed_ms(started),
            )
        )
        yield {"type": "done", "data": done_err}


async def iter_agent_sse_events_after_confirm(
    payload: AgentChatRequest,
    checkpoint: dict[str, Any],
    action_result: dict[str, Any],
    *,
    chunk_id: str,
) -> AsyncIterator[dict[str, Any]]:
    """用户确认后续跑：执行结果流式输出 + ReAct 继续。"""
    created = int(time.time())
    started = time.perf_counter()
    thinking = payload.thinking
    locale = get_locale_from_context(payload.context)
    role_sent = True
    full_content = ""
    full_reasoning = ""
    thinking_ms = 0

    action_type = str(checkpoint.get("action_type") or "")
    kind = str(checkpoint.get("kind") or "")
    mode = checkpoint.get("mode") or "react"
    intent = checkpoint.get("intent")
    steps = steps_from_dicts(checkpoint.get("steps") or [])

    result_text = format_action_result_text(action_type, action_result)
    workflow_data: dict[str, Any] = {}
    card_status = "confirmed" if action_result.get("success") else "failed"

    yield {
        "type": "block",
        "data": {
            "ops": [
                {
                    "op": "update_card",
                    "action_id": str(checkpoint.get("action_id") or ""),
                    "status": card_status,
                    "result_text": result_text,
                }
            ]
        },
    }

    if action_type == ACTION_TYPE_CREATE_JIRA and action_result.get("success"):
        workflow_data = dict(action_result)
        workflow_data["status"] = "created"
    elif not action_result.get("success"):
        workflow_data = {
            "success": False,
            "status": "failed",
            "error": action_result.get("error"),
        }

    async for delta in stream_plain_text(result_text):
        full_content += delta
        line = format_chunk(
            {"content": delta},
            chunk_id=chunk_id,
            created=created,
        )
        yield {"type": "sse", "data": {"line": line, "update_blocks": False}}

    if kind == "workflow":
        duration_ms = _elapsed_ms(started)
        line = format_agent_metadata_chunk(
            _build_stream_metadata(
                chunk_id=chunk_id,
                mode=str(mode),
                intent=intent,
                workflow_data=workflow_data,
                steps=steps,
                payload=payload,
                reasoning_content=full_reasoning if thinking else None,
                include_completed_at=True,
                thinking_ms=thinking_ms,
                duration_ms=duration_ms,
            ),
            chunk_id=chunk_id,
            created=created,
        )
        yield {"type": "sse", "data": {"line": line}}
        yield {"type": "sse", "data": {"line": format_chunk({}, finish_reason="stop", chunk_id=chunk_id, created=created)}}
        yield {"type": "sse", "data": {"line": format_stream_done()}}
        done_data: dict[str, Any] = {
            "content": full_content,
            "mode": mode,
            "intent": intent,
            "workflow_data": workflow_data,
            "reasoning_content": None,
            "steps": [
                {
                    "step": s.step,
                    "thought": s.thought,
                    "action": s.action,
                    "action_input": s.action_input,
                    "observation": s.observation,
                }
                for s in steps
            ],
        }
        done_data.update(_timing_fields(thinking_ms=thinking_ms, duration_ms=duration_ms))
        yield {"type": "done", "data": done_data}
        return

    messages = list(checkpoint.get("react_messages") or [])
    pending = checkpoint.get("pending_tool_call") or {}
    observation = action_result.get("observation") or action_result
    tool_name = pending.get("name") or action_type
    tool_call_id = pending.get("id") or ""
    action_input = action_result.get("draft") or checkpoint.get("draft") or {}

    if tool_call_id:
        append_tool_message(messages, tool_call_id, observation)
    steps.append(
        ReactStepRecord(
            step=int(checkpoint.get("step_idx") or 1),
            thought="",
            action=tool_name,
            action_input=action_input if isinstance(action_input, dict) else {},
            observation=observation if isinstance(observation, dict) else {"result": observation},
        )
    )
    batch_step = int(checkpoint.get("step_idx") or 1)
    selected_skill_ids = list(checkpoint.get("selected_skill_ids") or [])
    direct_reply = bool(checkpoint.get("direct_reply"))

    remaining_tool_calls = checkpoint.get("remaining_tool_calls")
    if not remaining_tool_calls and pending:
        remaining_tool_calls = [pending]

    pause_data = await process_remaining_tool_calls_after_confirm(
        payload,
        messages,
        steps,
        remaining_tool_calls or [],
        1 if remaining_tool_calls else 0,
        batch_step,
        selected_skill_ids=selected_skill_ids,
        direct_reply=direct_reply,
    )
    if pause_data:
        thinking_ms = _elapsed_ms(started)
        async for item in _yield_action_required_sse(
            pause_data,
            chunk_id=chunk_id,
            created=created,
            thinking=thinking,
            role_sent=role_sent,
            thinking_ms=thinking_ms,
            duration_ms=_elapsed_ms(started),
        ):
            yield item
        return

    draft_answer: str | None = None
    thinking_started = time.perf_counter()
    try:
        async for event in continue_react_loop(
            payload,
            messages,
            steps,
            batch_step + 1,
            skill_ids=selected_skill_ids,
        ):
            if event.type == "action_required":
                thinking_ms = _elapsed_ms(thinking_started)
                async for item in _yield_action_required_sse(
                    event.data,
                    chunk_id=chunk_id,
                    created=created,
                    thinking=thinking,
                    role_sent=role_sent,
                    thinking_ms=thinking_ms,
                    duration_ms=_elapsed_ms(started),
                ):
                    yield item
                return
            if event.type == "thought" and thinking:
                text = _format_reasoning_thought(event.data)
                if text.strip():
                    line = format_chunk(
                        {"reasoning_content": text},
                        chunk_id=chunk_id,
                        created=created,
                    )
                    yield {"type": "sse", "data": {"line": line}}
                    full_reasoning += text
            elif event.type == "tool_start" and thinking:
                tool_name_evt = event.data.get("tool_name") or ""
                text = format_thinking_tool_start(tool_name_evt, locale)
                line = format_chunk(
                    {"reasoning_content": text},
                    chunk_id=chunk_id,
                    created=created,
                )
                yield {"type": "sse", "data": {"line": line}}
                full_reasoning += text
            elif event.type == "tool_done" and thinking:
                tool_name_evt = event.data.get("tool_name") or ""
                text = format_thinking_tool_done(tool_name_evt, locale)
                line = format_chunk(
                    {"reasoning_content": text},
                    chunk_id=chunk_id,
                    created=created,
                )
                yield {"type": "sse", "data": {"line": line}}
                full_reasoning += text
            elif event.type == "finish":
                draft_answer = event.data.get("final_answer")
                steps[:] = steps_from_dicts(event.data.get("steps") or [])
                mode = event.data.get("mode") or "react"
            elif event.type == "max_steps":
                if thinking:
                    text = event.data.get("message") or format_thinking_max_steps(
                        payload.max_steps or 6,
                        locale,
                    )
                    line = format_chunk(
                        {"reasoning_content": text},
                        chunk_id=chunk_id,
                        created=created,
                    )
                    yield {"type": "sse", "data": {"line": line}}
                    full_reasoning += text
                steps[:] = steps_from_dicts(event.data.get("steps") or [])

        thinking_ms = _elapsed_ms(thinking_started)
        for item in _iter_seal_reasoning_events(
            thinking_ms,
            chunk_id=chunk_id,
            created=created,
        ):
            yield item
        temperature = payload.temperature or 0.7
        draft = (draft_answer or "").strip()
        if draft:
            delta_iter = stream_plain_text(draft)
        else:
            delta_iter = stream_final_answer(
                payload,
                steps=steps,
                draft_answer=draft_answer,
                temperature=temperature,
            )
        async for delta in delta_iter:
            line = format_chunk(
                {"content": delta},
                chunk_id=chunk_id,
                created=created,
            )
            yield {"type": "sse", "data": {"line": line}}
            full_content += delta

        duration_ms = _elapsed_ms(started)
        line = format_agent_metadata_chunk(
            _build_stream_metadata(
                chunk_id=chunk_id,
                mode=str(mode),
                intent=intent,
                workflow_data=workflow_data,
                steps=steps,
                payload=payload,
                reasoning_content=full_reasoning if thinking else None,
                include_completed_at=True,
                thinking_ms=thinking_ms,
                duration_ms=duration_ms,
            ),
            chunk_id=chunk_id,
            created=created,
        )
        yield {"type": "sse", "data": {"line": line}}
        yield {"type": "sse", "data": {"line": format_chunk({}, finish_reason="stop", chunk_id=chunk_id, created=created)}}
        yield {"type": "sse", "data": {"line": format_stream_done()}}
        done_react: dict[str, Any] = {
            "content": full_content,
            "mode": mode,
            "intent": intent,
            "workflow_data": workflow_data,
            "reasoning_content": full_reasoning if thinking else None,
            "steps": [
                {
                    "step": s.step,
                    "thought": s.thought,
                    "action": s.action,
                    "action_input": s.action_input,
                    "observation": s.observation,
                }
                for s in steps
            ],
        }
        done_react.update(_timing_fields(thinking_ms=thinking_ms, duration_ms=duration_ms))
        yield {"type": "done", "data": done_react}
    except Exception as exc:
        logger.error("确认后续跑失败", exc_info=True)
        err_text = str(exc)
        line = format_chunk({"content": err_text}, chunk_id=chunk_id, created=created)
        yield {"type": "sse", "data": {"line": line}}
        for item in _iter_seal_reasoning_events(
            thinking_ms or _elapsed_ms(thinking_started),
            chunk_id=chunk_id,
            created=created,
        ):
            yield item
        yield {"type": "sse", "data": {"line": format_chunk({}, finish_reason="stop", chunk_id=chunk_id, created=created)}}
        yield {"type": "sse", "data": {"line": format_stream_done()}}
        done_err: dict[str, Any] = {"content": full_content + err_text, "mode": mode, "workflow_data": {}}
        done_err.update(
            _timing_fields(
                thinking_ms=thinking_ms or _elapsed_ms(thinking_started),
                duration_ms=_elapsed_ms(started),
            )
        )
        yield {"type": "done", "data": done_err}


async def iter_agent_sse_events_after_cancel(
    checkpoint: dict[str, Any],
    cancel_text: str,
    *,
    chunk_id: str,
    payload: AgentChatRequest | None = None,
) -> AsyncIterator[dict[str, Any]]:
    created = int(time.time())
    started = time.perf_counter()
    thinking = bool(payload and payload.thinking)
    locale = get_locale_from_context(payload.context if payload else None)
    mode = checkpoint.get("mode") or "react"
    intent = checkpoint.get("intent")
    steps = steps_from_dicts(checkpoint.get("steps") or [])
    workflow_data = {
        "status": "cancelled",
        "action_id": checkpoint.get("action_id"),
        "action_type": checkpoint.get("action_type"),
    }
    full_content = ""
    full_reasoning = ""
    role_sent = True
    thinking_ms = 0

    cancel_status = "expired"
    user_response = checkpoint.get("user_response") or {}
    if user_response.get("reason") != "timeout":
        cancel_status = "cancelled"

    yield {
        "type": "block",
        "data": {
            "ops": [
                {
                    "op": "update_card",
                    "action_id": str(checkpoint.get("action_id") or ""),
                    "status": cancel_status,
                }
            ]
        },
    }

    async for delta in stream_plain_text(cancel_text):
        full_content += delta
        line = format_chunk({"content": delta}, chunk_id=chunk_id, created=created)
        yield {"type": "sse", "data": {"line": line}}

    kind = str(checkpoint.get("kind") or "")
    if kind == "react" and payload is not None:
        messages = list(checkpoint.get("react_messages") or [])
        pending_tool_call = checkpoint.get("pending_tool_call") or {}
        remaining_tool_calls = list(checkpoint.get("remaining_tool_calls") or [])
        if not remaining_tool_calls and pending_tool_call:
            remaining_tool_calls = [pending_tool_call]
        batch_step = int(checkpoint.get("step_idx") or 1)
        selected_skill_ids = list(checkpoint.get("selected_skill_ids") or [])
        direct_reply = bool(checkpoint.get("direct_reply"))

        pause_data = await process_after_pending_tool_cancelled(
            payload,
            messages,
            steps,
            remaining_tool_calls,
            pending_tool_call,
            batch_step,
            selected_skill_ids=selected_skill_ids,
            direct_reply=direct_reply,
        )
        if pause_data:
            thinking_ms = _elapsed_ms(started)
            async for item in _yield_action_required_sse(
                pause_data,
                chunk_id=chunk_id,
                created=created,
                thinking=thinking,
                role_sent=role_sent,
                thinking_ms=thinking_ms,
                duration_ms=_elapsed_ms(started),
            ):
                yield item
            return

        draft_answer: str | None = None
        thinking_started = time.perf_counter()
        try:
            async for event in continue_react_loop(
                payload,
                messages,
                steps,
                batch_step + 1,
                skill_ids=selected_skill_ids,
            ):
                if event.type == "action_required":
                    thinking_ms = _elapsed_ms(thinking_started)
                    async for item in _yield_action_required_sse(
                        event.data,
                        chunk_id=chunk_id,
                        created=created,
                        thinking=thinking,
                        role_sent=role_sent,
                        thinking_ms=thinking_ms,
                        duration_ms=_elapsed_ms(started),
                    ):
                        yield item
                    return
                if event.type == "thought" and thinking:
                    text = _format_reasoning_thought(event.data)
                    if text.strip():
                        line = format_chunk(
                            {"reasoning_content": text},
                            chunk_id=chunk_id,
                            created=created,
                        )
                        yield {"type": "sse", "data": {"line": line}}
                        full_reasoning += text
                elif event.type == "tool_start" and thinking:
                    tool_name_evt = event.data.get("tool_name") or ""
                    text = format_thinking_tool_start(tool_name_evt, locale)
                    line = format_chunk(
                        {"reasoning_content": text},
                        chunk_id=chunk_id,
                        created=created,
                    )
                    yield {"type": "sse", "data": {"line": line}}
                    full_reasoning += text
                elif event.type == "tool_done" and thinking:
                    tool_name_evt = event.data.get("tool_name") or ""
                    text = format_thinking_tool_done(tool_name_evt, locale)
                    line = format_chunk(
                        {"reasoning_content": text},
                        chunk_id=chunk_id,
                        created=created,
                    )
                    yield {"type": "sse", "data": {"line": line}}
                    full_reasoning += text
                elif event.type == "finish":
                    draft_answer = event.data.get("final_answer")
                    steps[:] = steps_from_dicts(event.data.get("steps") or [])
                    mode = event.data.get("mode") or "react"
                elif event.type == "max_steps":
                    if thinking:
                        text = event.data.get("message") or format_thinking_max_steps(
                        payload.max_steps or 6,
                        locale,
                    )
                        line = format_chunk(
                            {"reasoning_content": text},
                            chunk_id=chunk_id,
                            created=created,
                        )
                        yield {"type": "sse", "data": {"line": line}}
                        full_reasoning += text
                    steps[:] = steps_from_dicts(event.data.get("steps") or [])

            thinking_ms = _elapsed_ms(thinking_started)
            for item in _iter_seal_reasoning_events(
                thinking_ms,
                chunk_id=chunk_id,
                created=created,
            ):
                yield item
            temperature = payload.temperature or 0.7
            draft = (draft_answer or "").strip()
            if draft:
                delta_iter = stream_plain_text(draft)
            else:
                delta_iter = stream_final_answer(
                    payload,
                    steps=steps,
                    draft_answer=draft_answer,
                    temperature=temperature,
                )
            async for delta in delta_iter:
                line = format_chunk(
                    {"content": delta},
                    chunk_id=chunk_id,
                    created=created,
                )
                yield {"type": "sse", "data": {"line": line}}
                full_content += delta
        except Exception as exc:
            logger.error("取消后续跑失败", exc_info=True)
            err_text = str(exc)
            line = format_chunk({"content": err_text}, chunk_id=chunk_id, created=created)
            yield {"type": "sse", "data": {"line": line}}
            full_content += err_text
            thinking_ms = thinking_ms or _elapsed_ms(thinking_started)
            for item in _iter_seal_reasoning_events(
                thinking_ms,
                chunk_id=chunk_id,
                created=created,
            ):
                yield item

    duration_ms = _elapsed_ms(started)
    line = format_agent_metadata_chunk(
        _build_stream_metadata(
            chunk_id=chunk_id,
            mode=str(mode),
            intent=intent,
            workflow_data=workflow_data,
            steps=steps,
            payload=payload,
            reasoning_content=full_reasoning if thinking else None,
            include_completed_at=True,
            thinking_ms=thinking_ms,
            duration_ms=duration_ms,
        ),
        chunk_id=chunk_id,
        created=created,
    )
    yield {"type": "sse", "data": {"line": line}}
    yield {"type": "sse", "data": {"line": format_chunk({}, finish_reason="stop", chunk_id=chunk_id, created=created)}}
    yield {"type": "sse", "data": {"line": format_stream_done()}}
    done_data: dict[str, Any] = {
        "content": full_content,
        "mode": mode,
        "intent": intent,
        "workflow_data": workflow_data,
        "reasoning_content": full_reasoning if thinking else None,
        "steps": [
            {
                "step": s.step,
                "thought": s.thought,
                "action": s.action,
                "action_input": s.action_input,
                "observation": s.observation,
            }
            for s in steps
        ],
    }
    done_data.update(_timing_fields(thinking_ms=thinking_ms, duration_ms=duration_ms))
    yield {"type": "done", "data": done_data}


async def run_agent_stream(payload: AgentChatRequest) -> AsyncIterator[str]:
    """直连流式（无 session_id 时使用）。"""
    async for event in iter_agent_sse_events(payload):
        if event["type"] == "sse":
            yield event["data"]["line"]


async def run_agent_complete(payload: AgentChatRequest) -> dict[str, Any]:
    result: dict[str, Any] = {"content": "", "mode": "react", "steps": []}
    async for event in iter_agent_events(payload):
        if event["type"] == "error":
            raise RuntimeError(event["data"].get("message") or "Agent 执行失败")
        if event["type"] == "done":
            result = event["data"]
    return result
