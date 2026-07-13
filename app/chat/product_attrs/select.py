# -*- coding: utf-8 -*-
#
# 大模型从产品目录中筛选用户问题涉及的型号
#
import json
import re
from typing import Any

from common.logger import logger

from chat.llm import DEEPSEEK_MODEL, complete_json
from chat.product_attrs.service import (
    build_alias_lookup_maps,
    resolve_canonical_name,
)

MAX_SELECT_PRODUCTS = 30

PRODUCT_SELECT_SYSTEM = """你是云鲸产品型号识别助手。根据用户对话和给定产品目录，选出需要查询参数的产品型号。

目录中每项包含：
- product_name：规范产品名（输出 products 时必须使用此字段的完整值）
- product_category：设备品类（sweeper_machine 扫地机 / washing_machine 洗地机 / vacuum_cleaner 吸尘器 / mite_remover 除螨仪）
- region：地区（cn 国内 / ovs 海外）
- aliases：别名列表（用户常用别名提问，如 J6、Flow 2、逍遥003 等）

另会提供「别名映射」列表，格式为：别名 → 规范型号名

只输出 JSON，不要 markdown：

{
  "products": ["规范型号1", "规范型号2"],
  "confidence": 0.0-1.0,
  "reason": "简要说明"
}

规则：
1. 用户提到别名时，必须在 products 中输出对应的 product_name（规范名），不要输出别名
2. products 中每个名称必须等于目录中的 product_name，禁止编造
3. 用户对比多款时最多选 3 个；单款问题通常选 1 个
4. 用户未指明型号且无法从对话推断时，products 可为空数组
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


def _build_alias_index(catalog: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in catalog:
        name = str(item.get("product_name") or "").strip()
        if not name:
            continue
        aliases = item.get("aliases") or []
        alias_text = ", ".join(str(a).strip() for a in aliases if str(a).strip())
        if alias_text:
            lines.append(f"- {alias_text} → {name}")
        else:
            lines.append(f"- {name}")
    return "\n".join(lines)


def _resolve_llm_products(
    raw_products: list[Any],
    exact_map: dict[str, str],
    lowered_map: dict[str, str],
    catalog_set: set[str],
    max_products: int,
) -> tuple[list[str], list[str]]:
    products: list[str] = []
    rejected: list[str] = []
    for item in raw_products:
        name = str(item).strip()
        if not name:
            continue
        canonical = resolve_canonical_name(name, exact_map, lowered_map)
        if canonical and canonical in catalog_set:
            if canonical not in products:
                products.append(canonical)
        else:
            rejected.append(name)
    return products[:max_products], rejected


async def select_products_by_llm(
    user_question: str,
    catalog: list[dict[str, Any]],
    region: str,
    max_products: int = MAX_SELECT_PRODUCTS,
) -> dict[str, Any]:
    """调用快模型从目录中筛选规范 product_name。"""
    question = (user_question or "").strip()
    catalog = [c for c in catalog if c.get("product_name")]
    if not catalog:
        return {"products": [], "confidence": 0.0, "source": "empty_catalog"}

    exact_map, lowered_map = build_alias_lookup_maps(catalog)
    canonical_names = [item["product_name"] for item in catalog]
    catalog_set = set(canonical_names)

    alias_index = _build_alias_index(catalog)
    catalog_text = json.dumps(catalog, ensure_ascii=False)
    user_content = (
        f"区域（region）：{region or '未指定'}\n"
        f"对话内容（含历史）：\n{question}\n\n"
        f"别名映射（用户提到左侧别名时，输出右侧规范型号名）：\n{alias_index}\n\n"
        f"产品目录（含别名）：{catalog_text}"
    )

    try:
        response = await complete_json(
            messages=[
                {"role": "system", "content": PRODUCT_SELECT_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            temperature=0.1,
            model=DEEPSEEK_MODEL,
        )
        parsed = _parse_ai_json(response.choices[0].message.content or "")
        raw_products = parsed.get("products")
        if not isinstance(raw_products, list):
            raw_products = []

        products, rejected = _resolve_llm_products(
            raw_products,
            exact_map,
            lowered_map,
            catalog_set,
            max_products,
        )
        reason = str(parsed.get("reason") or "").strip()

        if rejected:
            logger.warning(f"型号识别 LLM 返回值未匹配目录: {rejected}")

        return {
            "products": products,
            "confidence": float(parsed.get("confidence") or 0.5),
            "source": "model",
            "reason": reason,
            "raw_products": raw_products,
            "rejected": rejected,
        }
    except Exception:
        logger.error("产品型号 LLM 筛选失败", exc_info=True)
        return {"products": [], "confidence": 0.0, "source": "error"}
