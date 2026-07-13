# -*- coding: utf-8 -*-
#
# Agent 会话与消息评价
#
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from async_outer_apis.ai_agent import FEEDBACK_CATEGORIES
from chat.memory_context import normalize_session_memory
from models.db_model import ChatAgentMessageFeedbackModel, ChatAgentSessionModel
from schema import AgentFeedbackVote

CHAT_SESSION_STATUS_ACTIVE = 1
CHAT_SESSION_STATUS_DELETED = 0


def _normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def _session_to_dict(row: ChatAgentSessionModel) -> dict[str, Any]:
    return {
        "session_id": row.session_id,
        "title": row.title or "",
        "status": int(row.status if row.status is not None else CHAT_SESSION_STATUS_ACTIVE),
        "messages": row.messages if isinstance(row.messages, list) else [],
        "memory": normalize_session_memory(getattr(row, "memory", None)),
        "creator": row.creator,
        "created_at": row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else None,
        "modified_at": row.modified_at.strftime("%Y-%m-%d %H:%M:%S") if row.modified_at else None,
    }


def _active_session_query(**filters: Any):
    return ChatAgentSessionModel.filter(
        status=CHAT_SESSION_STATUS_ACTIVE,
        **filters,
    )


def _session_list_item_to_dict(row: ChatAgentSessionModel) -> dict[str, Any]:
    messages = row.messages if isinstance(row.messages, list) else []
    return {
        "session_id": row.session_id,
        "title": row.title or "",
        "modified_at": row.modified_at.strftime("%Y-%m-%d %H:%M:%S") if row.modified_at else None,
        "message_count": len(messages),
    }


def _feedback_to_dict(row: ChatAgentMessageFeedbackModel) -> dict[str, Any]:
    return {
        "session_id": row.session_id,
        "message_id": row.message_id,
        "vote": int(row.vote) if row.vote is not None else None,
        "category": row.category,
        "comment": row.comment,
        "feedback_by": row.feedback_by,
        "feedback_at": row.feedback_at.strftime("%Y-%m-%d %H:%M:%S") if row.feedback_at else None,
    }


def _assistant_message_has_content(item: dict[str, Any]) -> bool:
    if str(item.get("content") or "").strip():
        return True
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    blocks = metadata.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return False
    from chat.message_blocks import flatten_blocks

    return bool(flatten_blocks(blocks).strip())


def is_persisted_conversation_message(item: Any) -> bool:
    """会话中可用于多轮上下文的历史消息（排除 streaming 占位等）。"""
    if not isinstance(item, dict):
        return False
    role = item.get("role")
    if role == "user":
        return bool(str(item.get("content") or "").strip())
    if role == "assistant":
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if metadata.get("status") == "streaming":
            return False
        return _assistant_message_has_content(item)
    return False


def filter_persisted_conversation_messages(messages: list[Any]) -> list[dict[str, Any]]:
    result = [dict(item) for item in messages if is_persisted_conversation_message(item)]
    # 去掉末尾未得到 assistant 回复的 user（如生成中断遗留）
    while result and result[-1].get("role") == "user":
        result.pop()
    return result


def extract_last_user_message(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in reversed(messages):
        if item.get("role") == "user" and str(item.get("content") or "").strip():
            return dict(item)
    return None


def extract_last_user_entry(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """末条 user 消息（content 可为空）。"""
    for item in reversed(messages):
        if item.get("role") == "user":
            return dict(item)
    return None


def is_empty_current_user_request(incoming_messages: list[dict[str, Any]]) -> bool:
    entry = extract_last_user_entry(incoming_messages)
    return entry is not None and not str(entry.get("content") or "").strip()


def _format_message_time() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def merge_session_request_messages(
    existing_messages: list[Any],
    incoming_messages: list[dict[str, Any]],
    *,
    allow_empty_current_user: bool = False,
) -> list[dict[str, Any]]:
    """
    将会话 DB 中的历史与本轮请求合并。

    前端可只提交最后一条 user；历史从 existing_messages 读取。
    若 DB 末条 user 与请求末条 user 内容相同，则不重复追加。
    allow_empty_current_user=True 时，空 content 仅用于 token 预估，不追加到历史。
    新追加的 user 消息会写入 created_at（提问时间）。
    """
    history = filter_persisted_conversation_messages(existing_messages)
    last_entry = extract_last_user_entry(incoming_messages)
    if not last_entry:
        raise ValueError("messages 最后一条必须是 user 角色")

    if allow_empty_current_user and not str(last_entry.get("content") or "").strip():
        return history

    last_user = extract_last_user_message(incoming_messages)
    if not last_user:
        raise ValueError("messages 最后一条必须是 user 角色且 content 不能为空")

    incoming_content = str(last_user.get("content") or "").strip()
    if history and history[-1].get("role") == "user":
        if str(history[-1].get("content") or "").strip() == incoming_content:
            return history
    user_message = dict(last_user)
    if not str(user_message.get("created_at") or "").strip():
        user_message["created_at"] = _format_message_time()
    return history + [user_message]


def conversation_messages_to_chat_messages(
    messages: list[dict[str, Any]],
) -> list[Any]:
    """将会话 dict 消息转为 AgentChatRequest 可用的 ChatMessage 列表。"""
    from chat.message_blocks import flatten_blocks
    from chat.schemas import ChatMessage

    result: list[ChatMessage] = []
    for item in messages:
        role = item.get("role")
        if role not in ("user", "assistant"):
            continue
        content = str(item.get("content") or "")
        if role == "assistant" and not content.strip():
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            blocks = metadata.get("blocks")
            if isinstance(blocks, list):
                content = flatten_blocks(blocks)
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else None
        result.append(
            ChatMessage(
                role=role,
                content=content,
                metadata=metadata,
            )
        )
    return result


def _find_message_in_session(messages: list[Any], message_id: str) -> dict[str, Any] | None:
    for item in messages:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            continue
        if str(metadata.get("id") or "") == message_id:
            return item
    return None


async def create_chat_session(
    creator: str,
    *,
    title: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    username = _normalize_username(creator)
    if not username:
        raise ValueError("无法识别当前用户")

    sid = (session_id or "").strip() or str(uuid.uuid4())
    exists = await ChatAgentSessionModel.filter(session_id=sid).exists()
    if exists:
        raise ValueError(f"会话ID已存在: {sid}")

    row = await ChatAgentSessionModel.create(
        session_id=sid,
        title=(title or "").strip() or None,
        messages=[],
        memory={},
        creator=username,
    )
    return _session_to_dict(row)


async def list_chat_sessions(
    creator: str,
    *,
    page: int = 1,
    per_page: int = 20,
) -> dict[str, Any]:
    username = _normalize_username(creator)
    if not username:
        raise ValueError("无法识别当前用户")

    page = max(page, 1)
    per_page = min(max(per_page, 1), 100)
    offset = (page - 1) * per_page

    query = _active_session_query(creator=username)
    total = await query.count()
    rows = await query.order_by("-modified_at", "-id").offset(offset).limit(per_page)

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "items": [_session_list_item_to_dict(row) for row in rows],
    }


async def get_chat_session(session_id: str, creator: str) -> dict[str, Any]:
    username = _normalize_username(creator)
    row = await _active_session_query(session_id=session_id, creator=username).first()
    if not row:
        raise ValueError("会话不存在或无权访问")
    data = _session_to_dict(row)

    from chat.generation_service import find_running_generation

    running = await find_running_generation(session_id, username)
    if running:
        data["active_generation"] = running

    return data


async def update_chat_session_title(
    session_id: str,
    creator: str,
    title: str,
) -> dict[str, Any]:
    username = _normalize_username(creator)
    row = await _active_session_query(session_id=session_id, creator=username).first()
    if not row:
        raise ValueError("会话不存在或无权访问")

    normalized_title = (title or "").strip() or None
    await ChatAgentSessionModel.filter(id=row.id).update(title=normalized_title)
    row = await ChatAgentSessionModel.get(id=row.id)

    return {
        "session_id": row.session_id,
        "title": row.title or "",
        "modified_at": row.modified_at.strftime("%Y-%m-%d %H:%M:%S") if row.modified_at else None,
    }


async def delete_chat_session(session_id: str, creator: str) -> dict[str, Any]:
    username = _normalize_username(creator)
    row = await ChatAgentSessionModel.filter(session_id=session_id, creator=username).first()
    if not row:
        raise ValueError("会话不存在或无权访问")
    if row.status == CHAT_SESSION_STATUS_DELETED:
        return {
            "session_id": session_id,
            "status": CHAT_SESSION_STATUS_DELETED,
            "already_deleted": True,
        }

    from chat.generation_service import cancel_generation, clear_session_running, find_running_generation

    running = await find_running_generation(session_id, username)
    if running and running.get("generation_id"):
        try:
            await cancel_generation(str(running["generation_id"]), username)
        except ValueError:
            await clear_session_running(username, session_id)
    else:
        await clear_session_running(username, session_id)

    await ChatAgentSessionModel.filter(id=row.id).update(status=CHAT_SESSION_STATUS_DELETED)

    return {
        "session_id": session_id,
        "status": CHAT_SESSION_STATUS_DELETED,
        "already_deleted": False,
    }


async def submit_message_feedback(
    session_id: str,
    message_id: str,
    vote: int,
    creator: str,
    *,
    category: str | None = None,
    comment: str | None = None,
) -> dict[str, Any]:
    if vote not in (AgentFeedbackVote.LIKE, AgentFeedbackVote.DISLIKE, AgentFeedbackVote.CANCEL):
        raise ValueError("vote 参数非法，仅支持 1 点赞 / -1 点踩 / 0 取消")
    if vote == AgentFeedbackVote.DISLIKE:
        if not category:
            raise ValueError("点踩时 category 不能为空")
        if category not in FEEDBACK_CATEGORIES:
            raise ValueError(f"category 无效，须为: {', '.join(FEEDBACK_CATEGORIES)}")

    username = _normalize_username(creator)
    session = await _active_session_query(session_id=session_id, creator=username).first()
    if not session:
        raise ValueError("会话不存在或无权访问")

    messages = session.messages if isinstance(session.messages, list) else []
    target = _find_message_in_session(messages, message_id)
    if not target:
        raise ValueError("未在会话消息列表中找到对应 message_id")
    if target.get("role") != "assistant":
        raise ValueError("仅支持对 assistant 消息评价")

    feedback_at = None if vote == AgentFeedbackVote.CANCEL else datetime.now()
    feedback_category = category if vote == AgentFeedbackVote.DISLIKE else None
    feedback_comment = comment if vote == AgentFeedbackVote.DISLIKE else None
    vote_value = None if vote == AgentFeedbackVote.CANCEL else AgentFeedbackVote(vote)

    existing = await ChatAgentMessageFeedbackModel.filter(message_id=message_id).first()
    if existing:
        await ChatAgentMessageFeedbackModel.filter(id=existing.id).update(
            session_id=session_id,
            vote=vote_value,
            category=feedback_category,
            comment=feedback_comment,
            feedback_by=username,
            feedback_at=feedback_at,
        )
        row = await ChatAgentMessageFeedbackModel.get(id=existing.id)
    else:
        if vote == AgentFeedbackVote.CANCEL:
            raise ValueError("尚未评价，无法取消")
        row = await ChatAgentMessageFeedbackModel.create(
            session_id=session_id,
            message_id=message_id,
            vote=vote_value,
            category=feedback_category,
            comment=feedback_comment,
            feedback_by=username,
            feedback_at=feedback_at,
        )

    return _feedback_to_dict(row)
