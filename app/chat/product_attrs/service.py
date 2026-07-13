# -*- coding: utf-8 -*-
#
# 产品参数 MySQL 查询（按 region 区分海内外）
#
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from common.logger import logger
from models.db_model import ChatProductAttrFieldModel, ChatProductAttrsModel
from pm_module.pm_attr_manage.field_keys import FIELD_KEY_TO_LABEL

RegionType = Literal["cn", "ovs", ""]
VALID_REGIONS = frozenset({"cn", "ovs"})

PRODUCT_ATTRS_JSON_PATH = Path(__file__).with_name("product_attrs.json")


def normalize_region(region: str | None) -> str:
    value = (region or "").strip().lower()
    if value in VALID_REGIONS:
        return value
    return ""


@lru_cache(maxsize=1)
def _load_json_fallback() -> dict[str, Any]:
    if not PRODUCT_ATTRS_JSON_PATH.is_file():
        return {}
    with open(PRODUCT_ATTRS_JSON_PATH, encoding="utf-8") as fp:
        data = json.load(fp)
    return data if isinstance(data, dict) else {}


def _normalize_aliases(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            except json.JSONDecodeError:
                pass
        return [text]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def build_alias_lookup_maps(
    catalog: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str]]:
    """构建精确与忽略大小写的别名 → product_name 映射。"""
    exact: dict[str, str] = {}
    lowered: dict[str, str] = {}
    for item in catalog:
        canonical = str(item.get("product_name") or "").strip()
        if not canonical:
            continue
        tokens = [canonical, *_normalize_aliases(item.get("aliases"))]
        for token in tokens:
            if not token:
                continue
            exact[token] = canonical
            lowered[token.lower()] = canonical
    return exact, lowered


def build_alias_map(catalog: list[dict[str, Any]]) -> dict[str, str]:
    """别名 / 型号名 → 规范 product_name。"""
    exact, _ = build_alias_lookup_maps(catalog)
    return exact


def resolve_canonical_name(
    name: str,
    exact_map: dict[str, str],
    lowered_map: dict[str, str],
) -> str | None:
    text = str(name).strip()
    if not text:
        return None
    if text in exact_map:
        return exact_map[text]
    return lowered_map.get(text.lower())


def format_product_alias_mapping(
    catalog: list[dict[str, Any]],
    products: list[str],
) -> str:
    """生成已选产品的规范名与别名对照，供最终回答使用。"""
    by_name = {
        str(item.get("product_name") or "").strip(): item
        for item in catalog
        if item.get("product_name")
    }
    lines: list[str] = []
    for name in products:
        item = by_name.get(name, {})
        aliases = _normalize_aliases(item.get("aliases"))
        alias_text = ", ".join(aliases) if aliases else "无额外别名"
        lines.append(f"- 规范型号「{name}」← 别名：{alias_text}")
    return "\n".join(lines) if lines else "无"


def canonicalize_product_names(names: list[str], alias_map: dict[str, str]) -> list[str]:
    """将用户输入或 LLM 返回的名称规范为 product_name。"""
    lowered = {key.lower(): value for key, value in alias_map.items()}
    result: list[str] = []
    seen: set[str] = set()
    for name in names:
        canonical = resolve_canonical_name(name, alias_map, lowered)
        if not canonical:
            canonical = str(name).strip()
        if canonical and canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result


async def list_product_catalog(region: str | None = None) -> list[dict[str, Any]]:
    """返回产品目录（含别名）。"""
    norm_region = normalize_region(region)
    try:
        query = ChatProductAttrsModel.filter(is_enabled=True)
        if norm_region:
            query = query.filter(region=norm_region)
        rows = await query.order_by("product_name").values(
            "product_name", "aliases", "product_category", "region"
        )
        if rows:
            merged: dict[str, dict[str, Any]] = {}
            for row in rows:
                name = str(row["product_name"] or "").strip()
                if not name:
                    continue
                aliases = _normalize_aliases(row.get("aliases"))
                row_region = normalize_region(row.get("region")) or norm_region or "cn"
                category = str(row.get("product_category") or "sweeper_machine").strip()
                if name in merged:
                    merged[name]["aliases"] = list(
                        dict.fromkeys(merged[name]["aliases"] + aliases)
                    )
                else:
                    merged[name] = {
                        "product_name": name,
                        "aliases": aliases,
                        "product_category": category,
                        "region": row_region,
                    }
            return list(merged.values())
    except Exception:
        logger.warning(f"查询产品目录失败，尝试 JSON 兜底 region={norm_region}", exc_info=True)

    if norm_region == "cn":
        return [
            {
                "product_name": name,
                "aliases": [],
                "product_category": "sweeper_machine",
                "region": norm_region or "cn",
            }
            for name in _load_json_fallback().keys()
        ]
    return []


async def list_product_names(region: str | None = None) -> list[str]:
    """列出某 region 下启用的规范产品型号。"""
    catalog = await list_product_catalog(region)
    return [item["product_name"] for item in catalog if item.get("product_name")]


async def get_attrs_by_names(
    region: str | None,
    product_names: list[str],
) -> dict[str, Any]:
    """按 region + 规范型号列表查询参数，返回 { product_name: attrs }。"""
    norm_region = normalize_region(region)
    names = [n.strip() for n in product_names if n and str(n).strip()]
    if not names:
        return {}

    result: dict[str, Any] = {}
    try:
        query = ChatProductAttrsModel.filter(
            product_name__in=names,
            is_enabled=True,
        )
        if norm_region:
            query = query.filter(region=norm_region)
        rows = await query.values("product_name", "attrs")
        for row in rows:
            result[row["product_name"]] = row["attrs"]
        if result:
            return result
    except Exception:
        logger.warning(
            f"按型号查询产品参数失败，尝试 JSON 兜底 region={norm_region} names={names}",
            exc_info=True,
        )

    if norm_region == "cn":
        fallback = _load_json_fallback()
        for name in names:
            if name in fallback:
                result[name] = fallback[name]
    return result


def resolve_products_from_context(context: dict[str, Any] | None) -> list[str] | None:
    """context.products / context.product 显式指定型号或别名。"""
    context = context or {}
    if context.get("products"):
        raw = context["products"]
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
    if context.get("product"):
        name = str(context["product"]).strip()
        return [name] if name else None
    return None


async def list_attr_field_catalog(region: str | None = None) -> list[dict[str, Any]]:
    """从 chat_product_attr_field 获取字段模板（供扫描模式选型）。"""
    norm_region = normalize_region(region)
    query = ChatProductAttrFieldModel.filter(is_enabled=True)
    if norm_region:
        query = query.filter(region__in=["all", norm_region])
    rows = await query.order_by("group_order", "sort_order", "id")

    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        field_key = str(row.field_key or "").strip()
        if not field_key:
            continue
        item = {
            "field_key": field_key,
            "label": row.label,
            "group_name": row.group_name,
            "group_order": row.group_order,
            "sort_order": row.sort_order,
            "placeholder": row.placeholder or "",
            "region": row.region,
        }
        existing = merged.get(field_key)
        if not existing:
            merged[field_key] = item
            continue
        if existing["region"] == "all" and row.region != "all":
            merged[field_key] = item
    return sorted(
        merged.values(),
        key=lambda x: (x["group_order"], x["sort_order"], x["field_key"]),
    )


def pick_attr_value(
    raw_attrs: dict[str, Any] | None,
    field_key: str,
    label: str | None = None,
) -> Any:
    """从 attrs 中取值，兼容英文 field_key 与中文 label。"""
    attrs = raw_attrs or {}
    if field_key in attrs:
        return attrs[field_key]
    text_label = (label or FIELD_KEY_TO_LABEL.get(field_key) or "").strip()
    if text_label and text_label in attrs:
        return attrs[text_label]
    return ""


def subset_attrs_by_fields(
    raw_attrs: dict[str, Any] | None,
    field_keys: list[str],
    field_meta: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """只保留选定字段，输出键为 field_key。"""
    result: dict[str, Any] = {}
    for field_key in field_keys:
        meta = field_meta.get(field_key, {})
        label = str(meta.get("label") or FIELD_KEY_TO_LABEL.get(field_key) or field_key)
        value = pick_attr_value(raw_attrs, field_key, label)
        if value is not None and value != "":
            result[field_key] = value
    return result


async def get_scan_attrs_by_fields(
    region: str | None,
    field_keys: list[str],
    field_meta: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """全库扫描：拉取所有产品指定字段的参数。"""
    if not field_keys:
        return {}
    field_meta = field_meta or {}
    all_names = await list_product_names(region)
    if not all_names:
        return {}
    full_attrs = await get_attrs_by_names(region, all_names)
    return {
        name: subset_attrs_by_fields(full_attrs.get(name), field_keys, field_meta)
        for name in all_names
        if name in full_attrs
    }


def format_field_descriptions(
    field_keys: list[str],
    field_meta: dict[str, dict[str, Any]],
) -> str:
    lines: list[str] = []
    for field_key in field_keys:
        meta = field_meta.get(field_key, {})
        label = meta.get("label") or FIELD_KEY_TO_LABEL.get(field_key, field_key)
        group = meta.get("group_name") or ""
        placeholder = meta.get("placeholder") or ""
        desc = f"- {label}（field_key: {field_key}"
        if group:
            desc += f"，分组: {group}"
        desc += "）"
        if placeholder:
            desc += f"：{placeholder}"
        lines.append(desc)
    return "\n".join(lines) if lines else "无"
