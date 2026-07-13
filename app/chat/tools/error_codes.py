# -*- coding: utf-8 -*-
#
# 十六进制系统错误码解析与规范化
#
from __future__ import annotations

import re

_HEX_ERROR_CODE_PATTERN = re.compile(r"0x[0-9a-fA-F]{7,8}", re.IGNORECASE)


def canonical_hex_error_code(raw: str) -> str | None:
    """将 0x 开头的 7/8 位十六进制错误码规范为 8 位（如 0x2100018 → 0x02100018）。"""
    text = (raw or "").strip()
    if not text:
        return None
    match = _HEX_ERROR_CODE_PATTERN.search(text)
    if not match:
        return None
    hex_part = match.group(0)[2:].lower()
    return f"0x{hex_part.zfill(8)}"


def expand_errorcode_search_variants(codes: list[str]) -> list[str]:
    """检索时同时匹配规范 8 位与省略前导零写法（如 0x02100018 / 0x2100018）。"""
    variants: list[str] = []
    seen: set[str] = set()
    for code in codes:
        hex_part = code[2:].lower() if code.lower().startswith("0x") else code.lower()
        canonical = f"0x{hex_part.zfill(8)}"
        candidates = [canonical]
        stripped = hex_part.lstrip("0") or "0"
        unpadded = f"0x{stripped}"
        if unpadded != canonical:
            candidates.append(unpadded)
        for item in candidates:
            if item not in seen:
                seen.add(item)
                variants.append(item)
    return variants


def extract_hex_error_codes_from_text(text: str) -> list[str]:
    """从文本中提取 16 进制系统错误码（如 0x02100018、0x2100018）。"""
    if not text:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for match in _HEX_ERROR_CODE_PATTERN.finditer(text):
        normalized = canonical_hex_error_code(match.group(0))
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result
