# -*- coding: utf-8 -*-
#
# 产品参数查询编排：先选型号，再查 MySQL（按 region）
#
import json
from typing import Any

from chat.product_attrs.field_select import select_fields_by_llm
from chat.product_attrs.select import select_products_by_llm
from chat.product_attrs.service import (
    build_alias_map,
    canonicalize_product_names,
    format_field_descriptions,
    format_product_alias_mapping,
    get_attrs_by_names,
    get_scan_attrs_by_fields,
    list_attr_field_catalog,
    list_product_catalog,
    normalize_region,
    resolve_products_from_context,
)


def _build_select_question(
    current_question: str,
    history_messages: list[dict[str, str]] | None = None,
) -> str:
    """拼接历史对话与当前问题，供型号识别使用。"""
    question = (current_question or "").strip()
    history = history_messages or []
    if not history:
        return question
    history_text = "\n".join(
        f"{m.get('role', 'user')}: {m.get('content', '')}" for m in history
    )
    return f"历史对话：\n{history_text}\n\n当前用户问题：{question}"


async def _query_scan_mode(
    region: str,
    select_question: str,
    catalog: list[dict[str, Any]],
    select_meta: dict[str, Any],
) -> dict[str, Any]:
    """方案 A：未指定型号时，由 LLM 选字段并全库拉取。"""
    field_catalog = await list_attr_field_catalog(region)
    if not field_catalog:
        return {
            "tool": "query_product_attrs",
            "region": region,
            "mode": "scan",
            "error": "无可用字段模板，无法全库筛选",
            "catalog_count": len(catalog),
            "select": select_meta,
            "products": [],
            "attrs": {},
        }

    field_meta = {str(f["field_key"]): f for f in field_catalog}
    field_select_meta = await select_fields_by_llm(
        select_question,
        field_catalog,
        region,
    )
    field_keys = field_select_meta.get("field_keys") or []
    if not field_keys:
        return {
            "tool": "query_product_attrs",
            "region": region,
            "mode": "scan",
            "error": "未能识别需要查询的参数项，请换一种问法或指明产品型号",
            "catalog_count": len(catalog),
            "select": select_meta,
            "field_select": field_select_meta,
            "products": [],
            "attrs": {},
        }

    all_products = [item["product_name"] for item in catalog if item.get("product_name")]
    attrs = await get_scan_attrs_by_fields(region, field_keys, field_meta)
    field_desc = format_field_descriptions(field_keys, field_meta)

    return {
        "tool": "query_product_attrs",
        "region": region,
        "mode": "scan",
        "products": all_products,
        "field_keys": field_keys,
        "field_descriptions": field_desc,
        "attrs": attrs,
        "missing_products": [p for p in all_products if p not in attrs],
        "catalog_count": len(catalog),
        "select": select_meta,
        "field_select": field_select_meta,
        "alias_mapping": "",
        "content": json.dumps(attrs, ensure_ascii=False),
    }


async def query_product_attrs_for_question(
    user_question: str,
    context: dict[str, Any] | None = None,
    history_messages: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """两阶段：解析型号 → 按 region 查库；无型号时走全库扫描。"""
    context = context or {}
    region = normalize_region(context.get("region"))
    question = (user_question or "").strip()

    catalog = await list_product_catalog(region)
    if not catalog:
        return {
            "tool": "query_product_attrs",
            "region": region,
            "mode": "specific",
            "error": "无可用产品参数数据" if not region else f"区域 {region} 下无可用产品参数数据",
            "products": [],
            "attrs": {},
        }

    alias_map = build_alias_map(catalog)
    explicit = resolve_products_from_context(context)
    select_meta: dict[str, Any] = {}
    history = history_messages or context.get("history_messages")
    select_question = _build_select_question(question, history)

    if explicit:
        products = canonicalize_product_names(explicit, alias_map)
        select_meta = {"source": "context", "confidence": 1.0, "products": products}
    else:
        select_meta = await select_products_by_llm(select_question, catalog, region)
        products = select_meta.get("products") or []

    if not products:
        return await _query_scan_mode(region, select_question, catalog, select_meta)

    attrs = await get_attrs_by_names(region, products)
    missing = [p for p in products if p not in attrs]
    alias_mapping = format_product_alias_mapping(catalog, products)

    return {
        "tool": "query_product_attrs",
        "region": region,
        "mode": "specific",
        "products": products,
        "attrs": attrs,
        "missing_products": missing,
        "catalog_count": len(catalog),
        "select": select_meta,
        "alias_mapping": alias_mapping,
        "content": json.dumps(attrs, ensure_ascii=False),
    }
