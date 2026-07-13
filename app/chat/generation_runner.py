# -*- coding: utf-8 -*-
#
# 后台执行 Agent 生成并写入内存缓冲 + Redis
#
from typing import Any

from common.logger import logger

from chat.agent import iter_agent_sse_events
from chat.generation_buffer import get_buffer
from chat.generation_buffer import get_buffer
from chat.generation_service import (
    abort_generation_as_cancelled,
    append_generation_chunk,
    apply_generation_block_ops,
    finalize_generation,
    GENERATION_STATUS_COMPLETED,
    GENERATION_STATUS_FAILED,
    GENERATION_STATUS_PAUSED,
    parse_sse_delta,
    pause_generation,
)
from chat.redis_generation import get_generation_meta
from chat.schemas import AgentChatRequest
from chat.stream_format import format_stream_done


async def run_generation_background(generation_id: str, payload: AgentChatRequest) -> None:
    buffer = get_buffer(generation_id)
    if buffer:
        chunk_id = buffer.chunk_id
    else:
        meta = await get_generation_meta(generation_id)
        if not meta:
            return
        chunk_id = meta.get("chunk_id") or ""

    final_mode: str | None = None
    final_intent: str | None = None
    final_workflow_data: dict[str, Any] = {}
    final_steps: list[Any] = []
    final_content = ""
    final_reasoning = ""
    final_thinking_ms: int | None = None
    final_duration_ms: int | None = None

    try:
        async for event in iter_agent_sse_events(payload, chunk_id=chunk_id):
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
                data = event.get("data") or {}
                block_ops = data.get("block_ops")
                if block_ops:
                    await apply_generation_block_ops(generation_id, block_ops)
                final_mode = data.get("mode")
                final_intent = data.get("intent")
                final_workflow_data = data.get("workflow_data") or {}
                final_steps = data.get("steps") or []
                final_content = data.get("content") or ""
                final_reasoning = data.get("reasoning_content") or ""
                final_thinking_ms = data.get("thinking_ms")
                final_duration_ms = data.get("duration_ms")
                if data.get("paused"):
                    buf = get_buffer(generation_id)
                    await pause_generation(
                        generation_id,
                        checkpoint=data.get("checkpoint") or {},
                        content=buf.content if buf else final_content,
                        reasoning_content=final_reasoning,
                        mode=final_mode,
                        intent=final_intent,
                        workflow_data=final_workflow_data,
                        steps=final_steps,
                        blocks=list(buf.blocks) if buf else None,
                        thinking_ms=final_thinking_ms,
                        duration_ms=final_duration_ms,
                    )
                    return

        buf = get_buffer(generation_id)
        await finalize_generation(
            generation_id,
            status=GENERATION_STATUS_COMPLETED,
            mode=final_mode,
            intent=final_intent,
            workflow_data=final_workflow_data,
            steps=final_steps,
            content=buf.content if buf else final_content,
            reasoning_content=final_reasoning or None,
            blocks=list(buf.blocks) if buf else None,
            thinking_ms=final_thinking_ms,
            duration_ms=final_duration_ms,
        )

    except Exception as exc:
        logger.error(f"后台生成失败 generation_id={generation_id}", exc_info=True)
        from chat.stream_format import format_chunk

        err_sse = event_error_sse(chunk_id, str(exc))
        await append_generation_chunk(generation_id, err_sse, content_delta=str(exc))
        stop_line = format_chunk({}, finish_reason="stop", chunk_id=chunk_id)
        await append_generation_chunk(generation_id, stop_line)
        await append_generation_chunk(generation_id, format_stream_done())
        await finalize_generation(
            generation_id,
            status=GENERATION_STATUS_FAILED,
            error=str(exc),
            content=str(exc),
        )


def event_error_sse(chunk_id: str, message: str) -> str:
    from chat.stream_format import format_chunk

    return format_chunk(
        {"role": "assistant", "content": message},
        chunk_id=chunk_id,
    )
