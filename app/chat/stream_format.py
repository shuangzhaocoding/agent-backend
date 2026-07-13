# -*- coding: utf-8 -*-
#
# OpenAI 兼容的 chat.completion.chunk 流式格式
#
import json
import time
from typing import Any

CHAT_COMPLETION_MODEL = "agent"


def format_chunk(
    delta: dict[str, Any],
    finish_reason: str | None = None,
    chunk_id: str = "1",
    created: int | None = None,
) -> str:
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created or int(time.time()),
        "model": CHAT_COMPLETION_MODEL,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def format_stream_done() -> str:
    return "data: [DONE]\n\n"


def format_agent_metadata_chunk(
    metadata: dict[str, Any],
    *,
    chunk_id: str = "1",
    created: int | None = None,
) -> str:
    """流式结束前输出 workflow_data 等元数据（delta.metadata）。"""
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created or int(time.time()),
        "model": CHAT_COMPLETION_MODEL,
        "choices": [
            {
                "index": 0,
                "delta": {"metadata": metadata},
                "finish_reason": None,
            }
        ],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def format_live_start_chunk(
    offset: int,
    *,
    chunk_id: str = "1",
    created: int | None = None,
) -> str:
    """历史回放结束、进入实时流的边界控制帧。"""
    return format_chunk(
        {"metadata": {"type": "live_start", "offset": offset}},
        chunk_id=chunk_id,
        created=created,
    )


def format_action_required_chunk(
    action_data: dict[str, Any],
    *,
    chunk_id: str = "1",
    created: int | None = None,
) -> str:
    """需用户确认的控制帧（delta.metadata.type=action_required）。"""
    metadata = {
        "type": "action_required",
        "action_id": action_data.get("action_id"),
        "action_type": action_data.get("action_type"),
        "title": action_data.get("title"),
        "draft": action_data.get("draft"),
        "kind": action_data.get("kind"),
        "respond_api": action_data.get("respond_api"),
        "status": "paused",
        "confirm_timeout_sec": action_data.get("confirm_timeout_sec"),
        "paused_at": action_data.get("paused_at"),
        "expires_at": action_data.get("expires_at"),
    }
    return format_chunk(
        {"metadata": metadata},
        chunk_id=chunk_id,
        created=created,
    )


def format_thinking_done_chunk(
    thinking_ms: int,
    *,
    chunk_id: str = "1",
    created: int | None = None,
) -> str:
    """本段思考结束控制帧（delta.metadata.type=thinking_done）。"""
    return format_chunk(
        {
            "metadata": {
                "type": "thinking_done",
                "thinking_ms": max(0, int(thinking_ms)),
            }
        },
        chunk_id=chunk_id,
        created=created,
    )


def format_generation_info_chunk(
    generation_id: str,
    message_id: str,
    *,
    chunk_id: str = "1",
    created: int | None = None,
    question_at: str | None = None,
) -> str:
    """流式开始时输出 generation_id / message_id，供前端续传。"""
    delta: dict[str, Any] = {
        "generation_id": generation_id,
        "message_id": message_id,
    }
    if question_at:
        delta["question_at"] = question_at
    return format_chunk(
        delta,
        chunk_id=chunk_id,
        created=created,
    )
