# -*- coding: utf-8 -*-
#
# 从 error_code_info 表查询系统错误码与 APP UI 错误码映射
#
from __future__ import annotations

import re
from typing import Any, Literal

from tortoise.transactions import in_transaction

from .error_codes import canonical_hex_error_code, expand_errorcode_search_variants

_UI_ERROR_CODE_PATTERN = re.compile(r"^\d{4}$")
_LOOKUP_COLUMNS = ("code", "appCode", "description", "solution", "strategy")

InputType = Literal["hex", "app_ui", "unknown"]


def classify_error_code_input(raw: str) -> tuple[str, InputType]:
    """识别输入为十六进制系统错误码或 4 位 APP UI 错误码。"""
    text = (raw or "").strip()
    if not text:
        return "", "unknown"

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
        "app_code": row.get("appCode"),
        "description": row.get("description"),
        "solution": row.get("solution"),
        "strategy": row.get("strategy"),
    }


async def _query_by_hex_codes(codes: list[str]) -> list[dict[str, Any]]:
    if not codes:
        return []

    variants: list[str] = []
    seen: set[str] = set()
    for code in codes:
        for item in expand_errorcode_search_variants([code]):
            lowered = item.lower()
            if lowered not in seen:
                seen.add(lowered)
                variants.append(lowered)

    placeholders = ", ".join("%s" for _ in variants)
    columns = ", ".join(f"`{name}`" for name in _LOOKUP_COLUMNS)
    sql = (
        f"SELECT {columns} FROM error_code_info "
        f"WHERE LOWER(code) IN ({placeholders})"
    )

    async with in_transaction("default") as conn:
        rows = await conn.execute_query_dict(sql, variants)
    return [_serialize_row(row) for row in rows]


async def _query_by_app_code(app_code: str) -> list[dict[str, Any]]:
    columns = ", ".join(f"`{name}`" for name in _LOOKUP_COLUMNS)
    sql = f"SELECT {columns} FROM error_code_info WHERE appCode = %s"
    params = [app_code]

    async with in_transaction("default") as conn:
        rows = await conn.execute_query_dict(sql, params)
        if rows:
            return [_serialize_row(row) for row in rows]

        if app_code.isdigit():
            rows = await conn.execute_query_dict(
                f"SELECT {columns} FROM error_code_info WHERE appCode = %s",
                [int(app_code)],
            )
    return [_serialize_row(row) for row in rows]


async def lookup_error_code_info(raw: str) -> dict[str, Any]:
    """按十六进制系统错误码或 4 位 APP UI 错误码查询 error_code_info。"""
    lookup_value, input_type = classify_error_code_input(raw)
    result: dict[str, Any] = {
        "input": (raw or "").strip(),
        "input_type": input_type,
        "lookup_value": lookup_value,
        "matched": False,
        "items": [],
        "hex_codes": [],
    }

    if input_type == "unknown" or not lookup_value:
        result["error"] = (
            "无法识别错误码：请提供 0x 开头的十六进制系统错误码（如 0x02100018）"
            "或 4 位 APP UI 错误码（如 2003）"
        )
        return result

    if input_type == "hex":
        items = await _query_by_hex_codes([lookup_value])
    else:
        items = await _query_by_app_code(lookup_value)

    result["items"] = items
    result["matched"] = bool(items)
    result["hex_codes"] = sorted(
        {
            str(item.get("code") or "").lower()
            for item in items
            if item.get("code")
        }
    )

    if not items:
        if input_type == "app_ui":
            result["error"] = f"未在 error_code_info 中找到 APP UI 错误码 {lookup_value}"
        else:
            result["error"] = f"未在 error_code_info 中找到系统错误码 {lookup_value}"

    return result
