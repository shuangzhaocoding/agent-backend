# -*- coding: utf-8 -*-
#
# 「你可能还想问」推荐追问生成
#
import json
import re
from typing import Any

from common.logger import logger

from chat.llm import DEEPSEEK_MODEL, complete_json
from chat.schemas import SuggestedQuestionsRequest

SUGGESTED_QUESTIONS_TITLE = "你可能还想问"

SUGGESTED_QUESTIONS_SYSTEM = """你是云鲸售后对话助手。根据对话历史与助手最新回答，生成用户可能还想继续追问的问题。

要求：
1. 仅输出与云鲸扫地/扫拖机器人售后技术支持相关的问题
2. 紧扣当前话题，有递进性（深入细节、关联排查、参数对比、设备信息等）
3. 每条一句话，可直接作为用户下一条输入，使用中文
4. 不要重复用户已经问过或助手已完整回答的问题
5. 按请求数量生成问题

只输出 JSON，不要 markdown：
{"questions": ["问题1", "问题2", "问题3"]}"""

MAX_ANSWER_CHARS = 6000
MAX_HISTORY_MESSAGES = 6
DEFAULT_QUESTION_COUNT = 3


def _parse_ai_json(text: str) -> dict[str, Any]:
    content = (text or "").strip()
    if not content:
        return {}
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                return {}
    return {}


def _normalize_questions(raw: Any, limit: int = DEFAULT_QUESTION_COUNT) -> list[str]:
    if not isinstance(raw, list):
        return []
    questions: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        questions.append(text)
        if len(questions) >= limit:
            break
    return questions


def _build_user_prompt(payload: SuggestedQuestionsRequest) -> str:
    history = payload.get_history_messages()
    if history:
        recent = history[-MAX_HISTORY_MESSAGES:]
        history_text = "\n".join(
            f"{m.get('role', 'user')}: {m.get('content', '')}" for m in recent
        )
    else:
        history_text = "（无历史对话）"

    answer = payload.get_assistant_answer()
    if len(answer) > MAX_ANSWER_CHARS:
        answer = answer[:MAX_ANSWER_CHARS] + "…"

    meta_parts = []
    if payload.intent:
        meta_parts.append(f"意图：{payload.intent}")
    if payload.mode:
        meta_parts.append(f"模式：{payload.mode}")
    meta_text = "；".join(meta_parts)

    lines = [
        f"历史对话：\n{history_text}",
        f"用户当前问题：{payload.get_current_user_content()}",
        f"需要生成推荐问题数量：{payload.count}",
    ]
    if meta_text:
        lines.append(meta_text)
    lines.append(f"助手最新回答：\n{answer or '（无）'}")
    return "\n\n".join(lines)


async def generate_suggested_questions(payload: SuggestedQuestionsRequest) -> list[str]:
    answer = payload.get_assistant_answer()
    if not answer:
        return []

    user_prompt = _build_user_prompt(payload)

    try:
        response = await complete_json(
            messages=[
                {"role": "system", "content": SUGGESTED_QUESTIONS_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.6,
            model=DEEPSEEK_MODEL,
        )
        parsed = _parse_ai_json(response.choices[0].message.content or "")
        return _normalize_questions(parsed.get("questions"), limit=payload.count)
    except Exception:
        logger.error("生成推荐问题失败", exc_info=True)
        return []


async def build_suggested_questions_response(payload: SuggestedQuestionsRequest) -> dict[str, Any]:
    questions = await generate_suggested_questions(payload)
    return {
        "title": SUGGESTED_QUESTIONS_TITLE,
        "questions": questions,
        "suggested_questions": questions,
    }
