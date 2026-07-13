# -*- coding: utf-8 -*-
#
# Skill 路由：create-jira 工作流分支 + 按问题关键词/意图加载部分 Skill
#
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from common.logger import logger

from chat.intent import classify_intent
from chat.schemas import AgentChatRequest
from chat.skills.loader import (
    CREATE_JIRA_SKILL_ID,
    discover_skills,
    match_skills_by_keywords,
    resolve_skills_for_query,
)


@dataclass
class SkillSelectionResult:
    skill_ids: list[str] = field(default_factory=list)
    direct_reply: bool = False
    confidence: float = 1.0
    source: str = "intent"
    elapsed_ms: int = 0


def _valid_skill_names() -> frozenset[str]:
    return frozenset(skill.name for skill in discover_skills())


def _normalize_skill_ids(raw: Any) -> list[str]:
    valid = _valid_skill_names()
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        if name in valid and name not in result:
            result.append(name)
    return result


def _build_query_text(payload: AgentChatRequest) -> str:
    text = payload.get_current_user_content()
    for msg in reversed(payload.get_history_messages()[-4:]):
        if msg.get("role") == "user":
            text = f"{msg.get('content', '')}\n{text}"
    return text


def _selection_from_legacy_intent(context: dict[str, Any]) -> SkillSelectionResult | None:
    """兼容 context.intent 强制指定（已废弃，请用 context.skill_ids）。"""
    intent = str(context.get("intent") or "").strip()
    if intent == "create_jira":
        return SkillSelectionResult(
            skill_ids=[CREATE_JIRA_SKILL_ID],
            direct_reply=False,
            confidence=1.0,
            source="context_intent",
        )
    return None


def _result_with_skills(
    skill_ids: list[str],
    *,
    confidence: float,
    source: str,
    elapsed_ms: int = 0,
) -> SkillSelectionResult:
    return SkillSelectionResult(
        skill_ids=skill_ids,
        direct_reply=False,
        confidence=confidence,
        source=source,
        elapsed_ms=elapsed_ms,
    )


async def select_skills(payload: AgentChatRequest) -> SkillSelectionResult:
    """create-jira 走工作流；其余按关键词/意图加载部分 Skill 与工具。"""
    context = payload.context or {}

    legacy = _selection_from_legacy_intent(context)
    if legacy:
        return legacy

    forced = context.get("skill_ids")
    if isinstance(forced, list) and forced:
        skill_ids = _normalize_skill_ids(forced)
        if CREATE_JIRA_SKILL_ID in skill_ids:
            return _result_with_skills(
                [CREATE_JIRA_SKILL_ID],
                confidence=1.0,
                source="context",
            )
        if skill_ids:
            return _result_with_skills(skill_ids, confidence=1.0, source="context")

    query_text = _build_query_text(payload)
    keyword_skills = match_skills_by_keywords(query_text)
    if keyword_skills:
        return _result_with_skills(
            keyword_skills,
            confidence=0.85,
            source="keyword",
        )

    started = time.perf_counter()
    try:
        intent_result = await classify_intent(payload)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if intent_result.intent == "create_jira":
            return _result_with_skills(
                [CREATE_JIRA_SKILL_ID],
                confidence=intent_result.confidence,
                source=f"intent:{intent_result.source}",
                elapsed_ms=elapsed_ms,
            )
        skill_ids = resolve_skills_for_query(
            query_text,
            intent=intent_result.intent,
        )
        return _result_with_skills(
            skill_ids,
            confidence=intent_result.confidence,
            source=f"intent:{intent_result.source}",
            elapsed_ms=elapsed_ms,
        )
    except Exception:
        logger.error("意图分类失败，回退全量 Skill", exc_info=True)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return _result_with_skills(
            resolve_skills_for_query(query_text),
            confidence=0.3,
            source="fallback",
            elapsed_ms=elapsed_ms,
        )


def build_skill_selection_event_data(result: SkillSelectionResult) -> dict[str, Any]:
    return {
        "skill_ids": result.skill_ids,
        "direct_reply": result.direct_reply,
        "confidence": result.confidence,
        "source": result.source,
        "elapsed_ms": result.elapsed_ms,
    }
