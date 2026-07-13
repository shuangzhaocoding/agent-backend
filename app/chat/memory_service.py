# -*- coding: utf-8 -*-
#
# 会话长期记忆：加载、注入、异步摘要更新
#
from __future__ import annotations

from datetime import datetime
from typing import Any

from common.logger import logger
from models.db_model import ChatAgentSessionModel

from chat.llm import DEEPSEEK_MODEL, get_deepseek_client
from chat.memory_context import (
    MAX_SUMMARY_CHARS,
    default_session_memory,
    format_messages_for_summary,
    normalize_session_memory,
    select_messages_to_summarize,
)
from chat.schemas import AgentChatRequest
from chat.session_service import filter_persisted_conversation_messages

SESSION_MEMORY_CONTEXT_KEY = "_session_memory"

SUMMARIZE_SYSTEM_PROMPT = """你是云鲸（Narwal）扫地/扫拖机器人售后对话的记忆助手。
请将「已有摘要」与「新增对话」合并压缩为简洁摘要，供后续多轮对话使用。
有已有摘要时：保留仍有效的信息，用新对话修正过时结论，不要丢弃未矛盾的旧事实。

必须保留（有则写，无则省略该条，勿编造）：

【设备与工单】
1. 设备标识：SN、DID、产品型号、固件/APP 版本、区域（cn/ovs）、关联码/三码结论
2. 关联业务单：售后工单号、Jira key、APPID、账单/提单相关编号
3. 用户问题与现象描述、发生时间、复现条件、环境线索（楼层/地毯/基站位置等，若提到）

【排查与决策】
4. 已执行的排查步骤与工具查询结论（用业务语言，不要贴 JSON）
5. 待确认/未完成事项（如 Jira 草稿、等待用户确认的推送/加白/BAG 开关、用户取消或超时的操作）
6. 用户已确认的关键决策，以及对错误结论的纠正（以最新为准）

【用户画像与偏好】
7. 用户角色/身份线索（如 FAE、客服、经销商、终端用户）及常用联系方式（邮箱等，若出现）
8. 沟通与回复偏好：语言（中/英）、详略程度、是否偏好步骤清单/话术、是否要求附带链接或截图说明
9. 操作偏好与约束：默认区域、常用机型简称、明确要求「不要做某事」（如勿推固件、勿建单、勿改 BAG）
10. 稳定事实与别名：用户对设备/问题的固定叫法、已确认的因果关系、排除项（已排除的原因）

要求：
- 使用中文，条理清晰；可按上述分组用短句罗列，不要 markdown 标题
- 优先保留对后续工具调用与决策有用的信息，删减寒暄与重复
- 控制在 800 字以内
- 只输出摘要正文"""


def read_session_memory_field(session: ChatAgentSessionModel) -> dict[str, Any]:
    raw = getattr(session, "memory", None)
    return normalize_session_memory(raw)


async def load_session_memory(session_id: str) -> dict[str, Any]:
    session = await ChatAgentSessionModel.filter(session_id=session_id).first()
    if not session:
        return default_session_memory()
    return read_session_memory_field(session)


def inject_session_memory_into_payload(
    payload: AgentChatRequest,
    memory: dict[str, Any],
) -> AgentChatRequest:
    context = dict(payload.context or {})
    context[SESSION_MEMORY_CONTEXT_KEY] = normalize_session_memory(memory)
    return payload.model_copy(update={"context": context})


def session_memory_from_context(context: dict[str, Any] | None) -> dict[str, Any] | None:
    if not context:
        return None
    if SESSION_MEMORY_CONTEXT_KEY not in context:
        return None
    return normalize_session_memory(context.get(SESSION_MEMORY_CONTEXT_KEY))


async def _call_summarization_llm(
    *,
    old_summary: str,
    conversation_text: str,
) -> str:
    user_parts = []
    if old_summary.strip():
        user_parts.append(f"已有摘要：\n{old_summary.strip()}")
    user_parts.append(f"新增对话：\n{conversation_text}")
    user_content = "\n\n".join(user_parts)

    client = get_deepseek_client()
    response = await client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": SUMMARIZE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        stream=False,
        temperature=0.2,
    )
    summary = (response.choices[0].message.content or "").strip()
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[:MAX_SUMMARY_CHARS] + "…"
    return summary


async def update_session_memory(session_id: str) -> dict[str, Any] | None:
    """将滑动窗口之外的消息增量折叠进 session.memory.summary。"""
    session = await ChatAgentSessionModel.filter(session_id=session_id).first()
    if not session:
        logger.warning(f"update_session_memory 会话不存在 session_id={session_id}")
        return None

    raw_messages = session.messages if isinstance(session.messages, list) else []
    persisted = filter_persisted_conversation_messages(raw_messages)
    memory = read_session_memory_field(session)

    to_summarize, new_until_index, _window_start = select_messages_to_summarize(
        persisted,
        memory,
    )
    if not to_summarize:
        return memory

    conversation_text = format_messages_for_summary(to_summarize)
    if not conversation_text.strip():
        return memory

    try:
        new_summary = await _call_summarization_llm(
            old_summary=str(memory.get("summary") or ""),
            conversation_text=conversation_text,
        )
    except Exception:
        logger.error(
            f"会话摘要生成失败 session_id={session_id}",
            exc_info=True,
        )
        return memory

    updated = {
        "summary": new_summary,
        "summarized_until_index": new_until_index,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    await ChatAgentSessionModel.filter(id=session.id).update(memory=updated)
    logger.info(
        f"会话摘要已更新 session_id={session_id} until_index={new_until_index} "
        f"summary_len={len(new_summary)}"
    )
    return updated


async def update_session_memory_async(session_id: str) -> None:
    await update_session_memory(session_id)
