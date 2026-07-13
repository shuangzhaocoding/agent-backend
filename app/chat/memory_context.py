# -*- coding: utf-8 -*-
#
# 会话长期记忆：滑动窗口 + 摘要前缀
#
from __future__ import annotations

from typing import Any

from chat.message_blocks import flatten_blocks
from chat.schemas import ChatMessage

# 保留最近 N 条 user/assistant 原文（约 6 轮）
SLIDING_WINDOW_MESSAGES = 12

# 超过该条数时，将更早消息折叠进摘要
SUMMARIZE_TRIGGER_MESSAGES = 12

MAX_SUMMARY_CHARS = 3000
MAX_MESSAGE_CHARS_FOR_LLM = 8000

SUMMARY_USER_PREFIX = "【会话摘要】"
SUMMARY_ASSISTANT_ACK = "好的，我已了解上述会话背景，将继续在此基础上协助你。"


def default_session_memory() -> dict[str, Any]:
    return {
        "summary": "",
        "summarized_until_index": 0,
    }


def normalize_session_memory(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return default_session_memory()
    summary = str(raw.get("summary") or "").strip()
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[:MAX_SUMMARY_CHARS] + "…"
    try:
        until_index = int(raw.get("summarized_until_index") or 0)
    except (TypeError, ValueError):
        until_index = 0
    return {
        "summary": summary,
        "summarized_until_index": max(until_index, 0),
        "updated_at": raw.get("updated_at"),
    }


def _truncate_text(text: str, limit: int = MAX_MESSAGE_CHARS_FOR_LLM) -> str:
    content = (text or "").strip()
    if len(content) <= limit:
        return content
    return content[:limit] + "…"


def message_dict_content_for_llm(msg: dict[str, Any]) -> str:
    role = msg.get("role")
    content = str(msg.get("content") or "")
    if role == "assistant" and not content.strip():
        metadata = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
        blocks = metadata.get("blocks")
        if isinstance(blocks, list):
            content = flatten_blocks(blocks)
    return _truncate_text(content)


def chat_message_content_for_llm(msg: ChatMessage) -> str:
    content = str(msg.content or "")
    if msg.role == "assistant" and not content.strip() and msg.metadata:
        blocks = msg.metadata.get("blocks")
        if isinstance(blocks, list):
            content = flatten_blocks(blocks)
    return _truncate_text(content)


def _history_to_llm_rows(history: list[Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in history:
        if isinstance(item, ChatMessage):
            role = item.role
            content = chat_message_content_for_llm(item)
        elif isinstance(item, dict):
            role = item.get("role")
            content = message_dict_content_for_llm(item)
        else:
            continue
        if role not in ("user", "assistant"):
            continue
        if not content:
            continue
        rows.append({"role": str(role), "content": content})
    return rows


def apply_sliding_window(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    if len(rows) <= SLIDING_WINDOW_MESSAGES:
        return rows
    return rows[-SLIDING_WINDOW_MESSAGES:]


def build_summary_prefix_messages(summary: str) -> list[dict[str, str]]:
    text = (summary or "").strip()
    if not text:
        return []
    return [
        {"role": "user", "content": f"{SUMMARY_USER_PREFIX}\n{text}"},
        {"role": "assistant", "content": SUMMARY_ASSISTANT_ACK},
    ]


def build_llm_history_messages(
    history: list[Any],
    memory: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """构建送入 LLM 的历史（摘要前缀 + 滑动窗口内的原文）。"""
    summary_msgs, recent_msgs = split_llm_history_for_usage(history, memory)
    return summary_msgs + recent_msgs


def split_llm_history_for_usage(
    history: list[Any],
    memory: dict[str, Any] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """拆分历史为 (摘要前缀消息, 滑动窗口消息)。"""
    window_rows = apply_sliding_window(_history_to_llm_rows(history))
    if not memory:
        return [], window_rows
    normalized = normalize_session_memory(memory)
    summary_rows = build_summary_prefix_messages(normalized.get("summary") or "")
    return summary_rows, window_rows


def select_messages_to_summarize(
    persisted_messages: list[dict[str, Any]],
    memory: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """
    选出需要折叠进摘要的消息切片。

    返回 (to_summarize, new_until_index, window_start_index)。
    """
    if len(persisted_messages) <= SUMMARIZE_TRIGGER_MESSAGES:
        return [], 0, len(persisted_messages)

    normalized = normalize_session_memory(memory)
    start = int(normalized.get("summarized_until_index") or 0)
    window_start = max(len(persisted_messages) - SLIDING_WINDOW_MESSAGES, 0)
    end = window_start
    if end <= start:
        return [], start, window_start

    to_summarize = [dict(item) for item in persisted_messages[start:end]]
    return to_summarize, end, window_start


def format_messages_for_summary(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in messages:
        role = item.get("role")
        if role not in ("user", "assistant"):
            continue
        content = message_dict_content_for_llm(item)
        if not content:
            continue
        label = "用户" if role == "user" else "助手"
        lines.append(f"{label}：{content}")
    return "\n\n".join(lines)
