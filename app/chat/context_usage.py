# -*- coding: utf-8 -*-
#
# 上下文 token 用量估算与分类
#
from __future__ import annotations

import json
from typing import Any

from chat.locale import get_locale_from_context
from chat.memory_context import (
    SLIDING_WINDOW_MESSAGES,
    SUMMARIZE_TRIGGER_MESSAGES,
    build_summary_prefix_messages,
    normalize_session_memory,
    split_llm_history_for_usage,
)
from chat.prompts import build_agent_system_prompt
from chat.schemas import AgentChatRequest
from chat.tools import build_openai_tools

# DeepSeek 最大上下文（1M）
DEEPSEEK_CONTEXT_LIMIT = 1_048_576

# 前端展示用建议安全线（非硬限制）
RECOMMENDED_CONTEXT_LIMIT = 128_000

CATEGORY_LABELS: dict[str, str] = {
    "system_prompt": "系统指令",
    "memory_summary": "长期记忆",
    "recent_messages": "近期对话",
    "current_user": "当前输入",
    "tools_schema": "工具定义",
    "react_runtime": "工具调用结果",
    "reasoning": "思考过程",
    "archived": "已折叠历史",
}


def estimate_tokens(text: str) -> int:
    """
    估算 token 数（中英混合 + JSON 启发式）。

    中文约 1.6 字符/token，ASCII/JSON 约 4 字符/token。
    """
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    other_chars = len(text) - ascii_chars
    tokens = other_chars / 1.6 + ascii_chars / 4.0
    return max(0, int(round(tokens)))


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for item in messages:
        content = str(item.get("content") or "")
        total += estimate_tokens(content)
        # 每条消息 role 等开销
        total += 4
        tool_calls = item.get("tool_calls")
        if tool_calls:
            total += estimate_tokens(json.dumps(tool_calls, ensure_ascii=False))
    return total


def estimate_tools_schema_tokens() -> int:
    return estimate_tokens(json.dumps(build_openai_tools(), ensure_ascii=False))


def estimate_react_runtime_tokens(steps: list[Any]) -> int:
    """从 ReAct steps 估算工具 observation 占用。"""
    if not steps:
        return 0
    total = 0
    for step in steps:
        if isinstance(step, dict):
            thought = step.get("thought") or ""
            action = step.get("action") or ""
            action_input = step.get("action_input")
            observation = step.get("observation")
        else:
            thought = getattr(step, "thought", "") or ""
            action = getattr(step, "action", "") or ""
            action_input = getattr(step, "action_input", None)
            observation = getattr(step, "observation", None)
        if thought:
            total += estimate_tokens(str(thought))
        if action:
            total += estimate_tokens(str(action))
        if action_input:
            total += estimate_tokens(json.dumps(action_input, ensure_ascii=False))
        if observation is not None:
            if isinstance(observation, str):
                total += estimate_tokens(observation)
            else:
                total += estimate_tokens(json.dumps(observation, ensure_ascii=False))
        total += 8
    return total


def _category_item(
    key: str,
    *,
    tokens: int,
    chars: int,
    message_count: int = 0,
    modes: list[str] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "key": key,
        "label": CATEGORY_LABELS.get(key, key),
        "tokens": tokens,
        "chars": chars,
    }
    if message_count:
        item["message_count"] = message_count
    if modes:
        item["modes"] = modes
    return item


def build_context_usage(
    payload: AgentChatRequest,
    *,
    mode: str = "react",
    react_steps: list[Any] | None = None,
    reasoning_content: str | None = None,
    session_stats: dict[str, Any] | None = None,
    preview_only: bool = False,
) -> dict[str, Any]:
    """
    构建分类上下文用量（发送前预估或生成后带 steps 峰值）。
    """
    locale = get_locale_from_context(payload.context)
    mode = (mode or "react").lower()
    system_text = build_agent_system_prompt(locale)

    chat_messages = [m for m in payload.messages if m.role != "system"]
    history_raw = chat_messages[:-1]
    summary_msgs, recent_msgs = split_llm_history_for_usage(
        history_raw,
        session_memory_from_payload(payload),
    )

    current_user = payload.get_current_user_content()
    files = payload.files or []
    if files:
        file_lines = [f"- {f.name}: {f.url}" for f in files]
        current_user = current_user + "\n\n附件：\n" + "\n".join(file_lines)

    system_tokens = estimate_tokens(system_text)
    summary_tokens = estimate_messages_tokens(summary_msgs)
    recent_tokens = estimate_messages_tokens(recent_msgs)
    current_tokens = estimate_tokens(current_user)
    tools_tokens = estimate_tools_schema_tokens() if mode == "react" else 0
    react_tokens = estimate_react_runtime_tokens(react_steps or []) if mode == "react" else 0
    reasoning_tokens = estimate_tokens(reasoning_content or "") if payload.thinking else 0

    categories: list[dict[str, Any]] = [
        _category_item(
            "system_prompt",
            tokens=system_tokens,
            chars=len(system_text),
        ),
    ]
    if summary_tokens:
        categories.append(
            _category_item(
                "memory_summary",
                tokens=summary_tokens,
                chars=sum(len(m.get("content") or "") for m in summary_msgs),
                message_count=len(summary_msgs),
            )
        )
    if recent_tokens:
        categories.append(
            _category_item(
                "recent_messages",
                tokens=recent_tokens,
                chars=sum(len(m.get("content") or "") for m in recent_msgs),
                message_count=len(recent_msgs),
            )
        )
    if current_tokens or not preview_only:
        categories.append(
            _category_item(
                "current_user",
                tokens=current_tokens,
                chars=len(current_user),
            )
        )
    if tools_tokens:
        categories.append(
            _category_item(
                "tools_schema",
                tokens=tools_tokens,
                chars=0,
                modes=["react"],
            )
        )
    if react_tokens:
        categories.append(
            _category_item(
                "react_runtime",
                tokens=react_tokens,
                chars=0,
                modes=["react"],
            )
        )
    if reasoning_tokens:
        categories.append(
            _category_item(
                "reasoning",
                tokens=reasoning_tokens,
                chars=len(reasoning_content or ""),
                modes=["thinking"],
            )
        )

    session_context_total = system_tokens + summary_tokens + recent_tokens + current_tokens
    if mode == "react":
        session_context_total += tools_tokens
    estimated_input = session_context_total + react_tokens + reasoning_tokens

    peak_input = estimated_input if react_tokens else session_context_total + reasoning_tokens

    usage_percent = round(estimated_input / DEEPSEEK_CONTEXT_LIMIT * 100, 2)
    recommended_percent = round(estimated_input / RECOMMENDED_CONTEXT_LIMIT * 100, 2)

    level = "normal"
    if usage_percent >= 80:
        level = "critical"
    elif usage_percent >= 50:
        level = "warning"
    elif usage_percent >= 20:
        level = "notice"

    result: dict[str, Any] = {
        "context_limit": DEEPSEEK_CONTEXT_LIMIT,
        "recommended_limit": RECOMMENDED_CONTEXT_LIMIT,
        "mode": mode,
        "thinking": bool(payload.thinking),
        "categories": categories,
        "total_estimated_input": estimated_input,
        "session_context_tokens": session_context_total,
        "peak_input_tokens": peak_input,
        "usage_percent": usage_percent,
        "recommended_usage_percent": recommended_percent,
        "level": level,
        "estimation_method": "heuristic_chars",
    }
    if preview_only:
        result["preview_only"] = True
    if session_stats:
        result["session_stats"] = session_stats
    if react_steps:
        result["react_steps"] = len(react_steps)
    return result


def session_memory_from_payload(payload: AgentChatRequest) -> dict[str, Any] | None:
    from chat.memory_service import session_memory_from_context

    return session_memory_from_context(payload.context)


def build_session_stats(
    persisted_messages: list[dict[str, Any]],
    memory: dict[str, Any] | None,
) -> dict[str, Any]:
    normalized = normalize_session_memory(memory)
    until_index = int(normalized.get("summarized_until_index") or 0)
    total = len(persisted_messages)
    window_start = max(total - SLIDING_WINDOW_MESSAGES, 0)
    in_window = min(total, SLIDING_WINDOW_MESSAGES) if total > 0 else 0
    archived_chars = len(str(normalized.get("summary") or ""))

    return {
        "messages_in_db": total,
        "messages_in_window": in_window,
        "messages_summarized": until_index,
        "messages_outside_window": window_start,
        "summarize_trigger": SUMMARIZE_TRIGGER_MESSAGES,
        "sliding_window_size": SLIDING_WINDOW_MESSAGES,
        "memory_summary_chars": archived_chars,
        "needs_summarize": total > SUMMARIZE_TRIGGER_MESSAGES,
    }
