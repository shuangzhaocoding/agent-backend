# -*- coding: utf-8 -*-
#
# 会话上下文 token 用量服务
#
from __future__ import annotations

from typing import Any

from models.db_model import ChatAgentSessionModel

from chat.context_usage import build_context_usage, build_session_stats
from chat.memory_service import inject_session_memory_into_payload, load_session_memory
from chat.schemas import AgentChatRequest
from chat.session_service import (
    CHAT_SESSION_STATUS_ACTIVE,
    _normalize_username,
    conversation_messages_to_chat_messages,
    filter_persisted_conversation_messages,
    is_empty_current_user_request,
    merge_session_request_messages,
)


def _messages_to_dicts(messages: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in messages:
        if hasattr(item, "model_dump"):
            row = item.model_dump(mode="json")
        elif isinstance(item, dict):
            row = dict(item)
        else:
            continue
        result.append(row)
    return result


async def compute_context_usage_for_payload(
    payload: AgentChatRequest,
    *,
    mode: str = "react",
    react_steps: list[Any] | None = None,
    reasoning_content: str | None = None,
    session_stats: dict[str, Any] | None = None,
    preview_only: bool = False,
) -> dict[str, Any]:
    return build_context_usage(
        payload,
        mode=mode,
        react_steps=react_steps,
        reasoning_content=reasoning_content,
        session_stats=session_stats,
        preview_only=preview_only,
    )


async def compute_context_usage_for_session(
    session_id: str,
    creator: str,
    payload: AgentChatRequest,
    *,
    mode: str = "react",
    react_steps: list[Any] | None = None,
    reasoning_content: str | None = None,
) -> dict[str, Any]:
    username = _normalize_username(creator)
    session = await ChatAgentSessionModel.filter(
        session_id=session_id,
        creator=username,
        status=CHAT_SESSION_STATUS_ACTIVE,
    ).first()
    if not session:
        raise ValueError("会话不存在或无权访问")

    existing_messages = session.messages if isinstance(session.messages, list) else []
    incoming_messages = _messages_to_dicts(payload.messages)
    preview_only = is_empty_current_user_request(incoming_messages)
    conversation = merge_session_request_messages(
        existing_messages,
        incoming_messages,
        allow_empty_current_user=preview_only,
    )

    memory = await load_session_memory(session_id)
    payload = inject_session_memory_into_payload(payload, memory)
    conversation_for_payload = conversation
    if preview_only:
        conversation_for_payload = conversation + [{"role": "user", "content": ""}]
    payload = payload.model_copy(
        update={"messages": conversation_messages_to_chat_messages(conversation_for_payload)},
    )

    persisted = filter_persisted_conversation_messages(existing_messages)
    session_stats = build_session_stats(persisted, memory)

    return await compute_context_usage_for_payload(
        payload,
        mode=mode,
        react_steps=react_steps,
        reasoning_content=reasoning_content,
        session_stats=session_stats,
        preview_only=preview_only,
    )


async def compute_context_usage_preview(
    payload: AgentChatRequest,
    *,
    mode: str = "react",
) -> dict[str, Any]:
    """无 session_id 时基于请求体 messages 预估。"""
    if payload.session_id:
        raise ValueError("请使用 compute_context_usage_for_session")
    return await compute_context_usage_for_payload(payload, mode=mode)
