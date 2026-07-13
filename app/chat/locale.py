# -*- coding: utf-8 -*-
#
# Chat 请求语言解析（Accept-Language / language 头）
#
from __future__ import annotations

from typing import Any

DEFAULT_LOCALE = "zh-CN"

SUPPORTED_LOCALES = frozenset({
    "zh-CN",
    "en-US",
    "ja-JP",
    "ko-KR",
    "de-DE",
})

_LOCALE_ALIASES = {
    "zh": "zh-CN",
    "cn": "zh-CN",
    "en": "en-US",
    "ja": "ja-JP",
    "jp": "ja-JP",
    "ko": "ko-KR",
    "kr": "ko-KR",
    "de": "de-DE",
}

_LOCALE_RESPONSE_RULES: dict[str, str] = {
    "zh-CN": (
        "使用简体中文回复，条理清晰，面向 FAE/客服场景。"
        "思考过程（reasoning_content）与正文使用相同语言：简体中文。"
    ),
    "en-US": (
        "Respond in English. Be clear and professional for FAE/support staff. "
        "Use English for both the final answer and reasoning_content (thinking)."
    ),
    "ja-JP": (
        "日本語で回答してください。FAE/サポート担当者向けに分かりやすく。"
        "思考過程（reasoning_content）も本文と同じ日本語で出力してください。"
    ),
    "ko-KR": (
        "한국어로 답변하세요. FAE/지원 담당자에게 명확하게 작성하세요. "
        "추론 과정(reasoning_content)도 본문과 같은 한국어로 작성하세요."
    ),
    "de-DE": (
        "Antworten Sie auf Deutsch. Klar und professionell für FAE/Support. "
        "Denkprozess (reasoning_content) in derselben Sprache wie die Antwort."
    ),
}

_THINKING_TOOL_START: dict[str, str] = {
    "zh-CN": "正在调用工具 {tool}...\n",
    "en-US": "Calling tool {tool}...\n",
    "ja-JP": "ツール {tool} を呼び出し中...\n",
    "ko-KR": "도구 {tool} 호출 중...\n",
    "de-DE": "Tool {tool} wird aufgerufen...\n",
}

_THINKING_TOOL_DONE: dict[str, str] = {
    "zh-CN": "工具 {tool} 执行完成\n",
    "en-US": "Tool {tool} completed\n",
    "ja-JP": "ツール {tool} の実行が完了\n",
    "ko-KR": "도구 {tool} 실행 완료\n",
    "de-DE": "Tool {tool} abgeschlossen\n",
}

_THINKING_MAX_STEPS: dict[str, str] = {
    "zh-CN": "已达最大推理步数 {max_steps}，将基于已有信息生成回答\n",
    "en-US": "Reached max reasoning steps ({max_steps}); answering from gathered info\n",
    "ja-JP": "最大推論ステップ数 {max_steps} に達しました。取得情報をもとに回答します\n",
    "ko-KR": "최대 추론 단계 {max_steps}에 도달했습니다. 수집 정보로 답변합니다\n",
    "de-DE": "Maximale Reasoning-Schritte ({max_steps}) erreicht; Antwort aus gesammelten Infos\n",
}

_THINKING_INTENT_CLASSIFYING: dict[str, str] = {
    "zh-CN": "正在识别用户意图…\n",
    "en-US": "Identifying user intent…\n",
    "ja-JP": "ユーザー意図を識別中…\n",
    "ko-KR": "사용자 의도를 식별하는 중…\n",
    "de-DE": "Benutzerabsicht wird erkannt…\n",
}

_THINKING_INTENT_CLASSIFIED: dict[str, str] = {
    "zh-CN": "意图识别：{intent_label}（{source_label}，置信度 {confidence:.0%}{elapsed}）\n",
    "en-US": "Intent: {intent_label} ({source_label}, confidence {confidence:.0%}{elapsed})\n",
    "ja-JP": "意図分類：{intent_label}（{source_label}、信頼度 {confidence:.0%}{elapsed}）\n",
    "ko-KR": "의도 분류: {intent_label} ({source_label}, 신뢰도 {confidence:.0%}{elapsed})\n",
    "de-DE": "Intent: {intent_label} ({source_label}, Konfidenz {confidence:.0%}{elapsed})\n",
}

_THINKING_SKILL_ROUTING: dict[str, str] = {
    "zh-CN": "正在识别用户意图…\n",
    "en-US": "Identifying user intent…\n",
    "ja-JP": "ユーザー意図を識別中…\n",
    "ko-KR": "사용자 의도를 식별하는 중…\n",
    "de-DE": "Benutzerabsicht wird erkannt…\n",
}

_THINKING_SKILLS_SELECTED: dict[str, str] = {
    "zh-CN": "意图识别：{skills_label}（{source_label}，置信度 {confidence:.0%}{elapsed}）\n",
    "en-US": "Intent: {skills_label} ({source_label}, confidence {confidence:.0%}{elapsed})\n",
    "ja-JP": "意図分類：{skills_label}（{source_label}，信頼度 {confidence:.0%}{elapsed}）\n",
    "ko-KR": "의도 분류: {skills_label} ({source_label}, 신뢰도 {confidence:.0%}{elapsed})\n",
    "de-DE": "Intent: {skills_label} ({source_label}, Konfidenz {confidence:.0%}{elapsed})\n",
}


def normalize_locale(value: str | None) -> str | None:
    if not value:
        return None
    token = str(value).strip().replace("_", "-")
    if not token:
        return None
    lower = token.lower()
    if lower in _LOCALE_ALIASES:
        return _LOCALE_ALIASES[lower]
    for locale in SUPPORTED_LOCALES:
        if locale.lower() == lower:
            return locale
    if "-" in token:
        lang = token.split("-", 1)[0].lower()
        if lang in _LOCALE_ALIASES:
            return _LOCALE_ALIASES[lang]
    return None


def parse_accept_language(header: str | None) -> str | None:
    if not header:
        return None
    for part in header.split(","):
        token = part.strip().split(";")[0].strip()
        locale = normalize_locale(token)
        if locale:
            return locale
    return None


def resolve_request_locale(
    accept_language: str | None = None,
    language_header: str | None = None,
    *,
    default: str = DEFAULT_LOCALE,
) -> str:
    """优先显式 language 头，其次 Accept-Language，默认 zh-CN。"""
    explicit = normalize_locale(language_header)
    if explicit:
        return explicit
    from_accept = parse_accept_language(accept_language)
    if from_accept:
        return from_accept
    return normalize_locale(default) or DEFAULT_LOCALE


def get_locale_from_context(context: dict[str, Any] | None) -> str:
    if not context:
        return DEFAULT_LOCALE
    for key in ("locale", "language", "lang"):
        locale = normalize_locale(context.get(key))
        if locale:
            return locale
    return DEFAULT_LOCALE


def build_locale_instruction(locale: str | None) -> str:
    code = normalize_locale(locale) or DEFAULT_LOCALE
    return _LOCALE_RESPONSE_RULES.get(code, _LOCALE_RESPONSE_RULES[DEFAULT_LOCALE])


def _locale_text(mapping: dict[str, str], locale: str | None, **kwargs: Any) -> str:
    code = normalize_locale(locale) or DEFAULT_LOCALE
    template = mapping.get(code, mapping[DEFAULT_LOCALE])
    return template.format(**kwargs)


def format_thinking_tool_start(tool_name: str, locale: str | None = None) -> str:
    return _locale_text(_THINKING_TOOL_START, locale, tool=tool_name or "")


def format_thinking_tool_done(tool_name: str, locale: str | None = None) -> str:
    return _locale_text(_THINKING_TOOL_DONE, locale, tool=tool_name or "")


def format_thinking_max_steps(max_steps: int, locale: str | None = None) -> str:
    return _locale_text(_THINKING_MAX_STEPS, locale, max_steps=max_steps)


def format_thinking_intent_classifying(locale: str | None = None) -> str:
    return _locale_text(_THINKING_INTENT_CLASSIFYING, locale)


def format_thinking_intent_classified(
    intent_data: dict[str, Any],
    locale: str | None = None,
) -> str:
    elapsed_ms = intent_data.get("elapsed_ms")
    elapsed_suffix = ""
    if isinstance(elapsed_ms, (int, float)) and elapsed_ms >= 0:
        if elapsed_ms >= 1000:
            elapsed_suffix = f"，{elapsed_ms / 1000:.1f}s"
        else:
            elapsed_suffix = f"，{int(elapsed_ms)}ms"
    return _locale_text(
        _THINKING_INTENT_CLASSIFIED,
        locale,
        intent_label=intent_data.get("intent_label") or intent_data.get("intent") or "",
        source_label=intent_data.get("source_label") or intent_data.get("source") or "",
        confidence=float(intent_data.get("confidence") or 0),
        elapsed=elapsed_suffix,
    )


def _format_elapsed_suffix(elapsed_ms: Any) -> str:
    if not isinstance(elapsed_ms, (int, float)) or elapsed_ms < 0:
        return ""
    if elapsed_ms >= 1000:
        return f"，{elapsed_ms / 1000:.1f}s"
    return f"，{int(elapsed_ms)}ms"


def format_thinking_skill_routing(locale: str | None = None) -> str:
    return _locale_text(_THINKING_SKILL_ROUTING, locale)


def format_thinking_skills_selected(
    skill_data: dict[str, Any],
    locale: str | None = None,
) -> str:
    skill_ids = skill_data.get("skill_ids") or []
    if skill_data.get("direct_reply"):
        skills_label = "直接回复（无工具）"
    elif skill_ids:
        skills_label = ", ".join(skill_ids)
    else:
        skills_label = "无"
    return _locale_text(
        _THINKING_SKILLS_SELECTED,
        locale,
        skills_label=skills_label,
        source_label=skill_data.get("source") or "",
        confidence=float(skill_data.get("confidence") or 0),
        elapsed=_format_elapsed_suffix(skill_data.get("elapsed_ms")),
    )


def apply_locale_to_context(
    context: dict[str, Any] | None,
    locale: str,
) -> dict[str, Any]:
    merged = dict(context or {})
    merged["locale"] = normalize_locale(locale) or DEFAULT_LOCALE
    return merged
