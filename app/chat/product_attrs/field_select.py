# -*- coding: utf-8 -*-
#
# 大模型从字段模板中筛选回答问题所需的属性
#
import json
import re
from typing import Any

from common.logger import logger

from chat.llm import DEEPSEEK_MODEL, complete_json

MAX_SELECT_FIELDS = 12

FIELD_SELECT_SYSTEM = """你是云鲸产品参数字段选择助手。用户未指定具体产品型号，需要在全产品库中按条件筛选或对比。
请根据用户问题，从给定字段列表中选出回答问题所必需的属性 field_key。

字段列表每项包含：
- field_key：attrs JSON 中的键（输出时必须使用此值）
- label：中文名称
- group_name：分组
- placeholder：填写说明（若有）

只输出 JSON，不要 markdown：

{
  "field_keys": ["max_suction", "battery_capacity"],
  "confidence": 0.0-1.0,
  "reason": "简要说明"
}

规则：
1. field_keys 必须来自给定列表，禁止编造
2. 只选与问题直接相关的字段，通常 1～5 个，最多不超过 12 个
3. 筛选/比较类问题（如「吸力大于多少」「哪些机型续航最长」）务必包含对应参数字段
4. 若问题涉及多款参数对比，选出所有被提及或用于排序/筛选的字段
5. 只输出 JSON"""


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


def _resolve_field_keys(
    raw_keys: list[Any],
    valid_keys: set[str],
    max_fields: int,
) -> tuple[list[str], list[str]]:
    selected: list[str] = []
    rejected: list[str] = []
    for item in raw_keys:
        key = str(item).strip()
        if not key:
            continue
        if key in valid_keys and key not in selected:
            selected.append(key)
        else:
            rejected.append(key)
    return selected[:max_fields], rejected


async def select_fields_by_llm(
    user_question: str,
    field_catalog: list[dict[str, Any]],
    region: str,
    max_fields: int = MAX_SELECT_FIELDS,
) -> dict[str, Any]:
    """调用快模型从 chat_product_attr_field 模板中筛选 field_key。"""
    question = (user_question or "").strip()
    fields = [f for f in field_catalog if f.get("field_key")]
    if not fields:
        return {"field_keys": [], "confidence": 0.0, "source": "empty_field_catalog"}

    valid_keys = {str(f["field_key"]) for f in fields}
    catalog_text = json.dumps(fields, ensure_ascii=False)
    user_content = (
        f"区域（region）：{region or '未指定'}\n"
        f"用户问题（含历史）：\n{question}\n\n"
        f"可选字段列表：{catalog_text}"
    )

    try:
        response = await complete_json(
            messages=[
                {"role": "system", "content": FIELD_SELECT_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            temperature=0.1,
            model=DEEPSEEK_MODEL,
        )
        parsed = _parse_ai_json(response.choices[0].message.content or "")
        raw_keys = parsed.get("field_keys")
        if not isinstance(raw_keys, list):
            raw_keys = []

        field_keys, rejected = _resolve_field_keys(raw_keys, valid_keys, max_fields)
        if rejected:
            logger.warning(f"字段识别 LLM 返回值未匹配模板: {rejected}")

        return {
            "field_keys": field_keys,
            "confidence": float(parsed.get("confidence") or 0.5),
            "source": "model",
            "reason": str(parsed.get("reason") or "").strip(),
            "raw_field_keys": raw_keys,
            "rejected": rejected,
        }
    except Exception:
        logger.error("参数字段 LLM 筛选失败", exc_info=True)
        return {"field_keys": [], "confidence": 0.0, "source": "error"}
