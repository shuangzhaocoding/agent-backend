# -*- coding: utf-8 -*-
#
# 从 voice_code_info 表查询语音错误码详情
#
from __future__ import annotations

import re
from typing import Any, Literal

from tortoise.transactions import in_transaction

from .error_codes import canonical_hex_error_code, expand_errorcode_search_variants

_VOICE_CODE_PATTERN = re.compile(r"^voice[A-Za-z0-9]+$", re.IGNORECASE)
_VOICE_CODE_IN_TEXT_PATTERN = re.compile(r"voice[A-Za-z0-9]+", re.IGNORECASE)
_UI_ERROR_CODE_PATTERN = re.compile(r"^\d{4}$")
_LOOKUP_COLUMNS = (
    "code",
    "sourceContent",
    "displayText",
    "errorCode",
    "appCode",
    "appMessage",
    "solution",
    "checkScheme",
    "helpCenterTitle",
    "group",
    "scene",
    "severity",
    "strategy",
)

InputType = Literal["voice_code", "hex", "app_ui", "unknown"]


def normalize_voice_code(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    if _VOICE_CODE_PATTERN.match(text):
        return text
    match = _VOICE_CODE_IN_TEXT_PATTERN.search(text)
    if match:
        return match.group(0)
    digits = re.sub(r"\D", "", text)
    if digits and _UI_ERROR_CODE_PATTERN.match(digits) is False and text.isdigit():
        return f"voice{digits}"
    return None


def classify_voice_code_input(raw: str) -> tuple[str, InputType]:
    text = (raw or "").strip()
    if not text:
        return "", "unknown"

    voice_code = normalize_voice_code(text)
    if voice_code:
        return voice_code, "voice_code"

    hex_code = canonical_hex_error_code(text)
    if hex_code:
        return hex_code, "hex"

    digits = re.sub(r"\D", "", text)
    if _UI_ERROR_CODE_PATTERN.match(digits):
        return digits, "app_ui"

    return text, "unknown"


def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": row.get("code"),
        "source_content": row.get("sourceContent"),
        "display_text": row.get("displayText"),
        "error_code": row.get("errorCode"),
        "app_code": row.get("appCode"),
        "app_message": row.get("appMessage"),
        "solution": row.get("solution"),
        "check_scheme": row.get("checkScheme"),
        "help_center_title": row.get("helpCenterTitle"),
        "group": row.get("group"),
        "scene": row.get("scene"),
        "severity": row.get("severity"),
        "strategy": row.get("strategy"),
    }


async def _query_by_voice_code(code: str) -> list[dict[str, Any]]:
    columns = ", ".join(f"`{name}`" for name in _LOOKUP_COLUMNS)
    sql = (
        f"SELECT {columns} FROM voice_code_info "
        "WHERE code = %s OR LOWER(code) = LOWER(%s)"
    )
    async with in_transaction("default") as conn:
        rows = await conn.execute_query_dict(sql, [code, code])
    return [_serialize_row(row) for row in rows]


async def _query_by_error_code(hex_code: str) -> list[dict[str, Any]]:
    variants: list[str] = []
    seen: set[str] = set()
    for item in expand_errorcode_search_variants([hex_code]):
        lowered = item.lower()
        if lowered not in seen:
            seen.add(lowered)
            variants.append(lowered)

    placeholders = ", ".join("%s" for _ in variants)
    columns = ", ".join(f"`{name}`" for name in _LOOKUP_COLUMNS)
    sql = (
        f"SELECT {columns} FROM voice_code_info "
        f"WHERE LOWER(errorCode) IN ({placeholders})"
    )
    async with in_transaction("default") as conn:
        rows = await conn.execute_query_dict(sql, variants)
    return [_serialize_row(row) for row in rows]


async def _query_by_app_code(app_code: str) -> list[dict[str, Any]]:
    columns = ", ".join(f"`{name}`" for name in _LOOKUP_COLUMNS)
    sql = f"SELECT {columns} FROM voice_code_info WHERE appCode = %s"
    async with in_transaction("default") as conn:
        rows = await conn.execute_query_dict(sql, [app_code])
        if rows:
            return [_serialize_row(row) for row in rows]
        if app_code.isdigit():
            rows = await conn.execute_query_dict(
                f"SELECT {columns} FROM voice_code_info WHERE appCode = %s",
                [int(app_code)],
            )
    return [_serialize_row(row) for row in rows]


async def lookup_voice_code_info(raw: str) -> dict[str, Any]:
    """按语音错误码、关联十六进制错误码或 APP UI 错误码查询 voice_code_info。"""
    lookup_value, input_type = classify_voice_code_input(raw)
    result: dict[str, Any] = {
        "input": (raw or "").strip(),
        "input_type": input_type,
        "lookup_value": lookup_value,
        "matched": False,
        "items": [],
        "voice_codes": [],
    }

    if input_type == "unknown" or not lookup_value:
        result["error"] = (
            "无法识别语音错误码：请提供 voice 开头的语音码（如 voice451、voiceK16），"
            "或关联的十六进制系统错误码（如 0x02100018），"
            "或 4 位 APP UI 错误码（如 2003）"
        )
        return result

    if input_type == "voice_code":
        items = await _query_by_voice_code(lookup_value)
    elif input_type == "hex":
        items = await _query_by_error_code(lookup_value)
    else:
        items = await _query_by_app_code(lookup_value)

    result["items"] = items
    result["matched"] = bool(items)
    result["voice_codes"] = sorted(
        {
            str(item.get("code") or "")
            for item in items
            if item.get("code")
        }
    )

    if not items:
        if input_type == "voice_code":
            result["error"] = f"未在 voice_code_info 中找到语音错误码 {lookup_value}"
        elif input_type == "app_ui":
            result["error"] = f"未在 voice_code_info 中找到 APP UI 错误码 {lookup_value}"
        else:
            result["error"] = f"未在 voice_code_info 中找到关联系统错误码 {lookup_value}"

    return result
