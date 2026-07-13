# -*- coding: utf-8 -*-
#
# SOP 知识库检索：解析过滤条件 → 向量召回 → 重排 → LLM 总结
#
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from common.logger import logger
from product_config import get_product_mapping
from schema import DeviceType

from chat.tools.error_codes import (
    canonical_hex_error_code,
    expand_errorcode_search_variants,
    extract_hex_error_codes_from_text,
)
from chat.locale import get_locale_from_context
from chat.llm import DEEPSEEK_MODEL, get_deepseek_client
from chat.product_attrs.select import select_products_by_llm
from chat.product_attrs.service import (
    build_alias_map,
    canonicalize_product_names,
    list_product_catalog,
    normalize_region,
)
from chat.sop_vector.config import load_sop_vector_settings
from chat.sop_vector.vector_index import SopVectorIndex

VECTOR_RECALL_TOP_K = 10
RERANK_TOP_K = 3
DEFAULT_SEARCH_REGION = "cn"

_CATEGORY_TO_DEVICE_TYPE: dict[str, int] = {
    "sweeper_machine": int(DeviceType.SweeperMachine),
    "washing_machine": int(DeviceType.WashingMachine),
    "vacuum_cleaner": int(DeviceType.VacuumCleaner),
    "mite_remover": int(DeviceType.MiteRemover),
}

_DEVICE_KEYWORDS: list[tuple[DeviceType, list[str]]] = [
    (DeviceType.SweeperMachine, ["扫地机", "扫地机器人", "扫拖机器人", "扫拖机", "拖地机器人"]),
    (DeviceType.WashingMachine, ["洗地机"]),
    (DeviceType.VacuumCleaner, ["吸尘器"]),
    (DeviceType.MiteRemover, ["除螨仪"]),
]

KNOWLEDGE_SUMMARY_SYSTEM = """你是云鲸（Narwal）售后技术支持助手。
根据检索到的排障知识片段，针对用户问题给出简洁、可执行的排查建议或客服话术要点。

规则：
1. 只依据提供的知识片段作答，不要编造未出现的信息
2. 条理清晰，优先给出排查步骤
3. 知识片段中可能包含已格式化的资源链接，须识别并完整保留，不得省略或改写 URL：
   - 图片：Markdown 语法 `![描述](https://...)`，原样保留
   - 视频：HTML `<video ...><source src="https://..."></video>`，原样保留
   - 文档/其他文件：Markdown 链接 `[文件名或URL](https://...)` 或 HTML `<a href="https://...">`，原样保留
   - 纯文本 https 链接：识别为资源时按上述对应格式输出
4. 若知识片段不足以回答，如实说明并建议补充产品型号或故障现象"""

_IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "gif", "bmp", "webp", "svg"})
_VIDEO_EXTENSIONS = frozenset({"mp4", "avi", "mov", "wmv", "rmvb", "mkv", "m4v", "webm"})
_FILE_EXTENSIONS = frozenset(
    {
        "pdf",
        "doc",
        "docx",
        "xls",
        "xlsx",
        "ppt",
        "pptx",
        "zip",
        "rar",
        "7z",
        "txt",
        "csv",
    }
)
_RESOURCE_EXTENSIONS = _IMAGE_EXTENSIONS | _VIDEO_EXTENSIONS | _FILE_EXTENSIONS

# 支持 http(s)、//、/ 路径，以及无协议头的域名路径
_URL_PATTERN = re.compile(
    r"(?:"
    r"https?://[^\s\)\]>\"']+"
    r"|//[^\s\)\]>\"']+"
    r"|/[^\s\)\]>\"']+"
    r"|(?<![/\w])(?:[\w\-]+\.)+[\w\-]{2,}/[^\s\)\]>\"']+"
    r")",
    re.IGNORECASE,
)
_FORMATTED_SEGMENT_PATTERN = re.compile(
    r"(!\[[^\]]*\]\([^)]+\)"
    r"|\[[^\]]+\]\([^)]+\)"
    r"|<(?:video|a|img|source)\b[^>]*>"
    r"|</(?:video|a)>)",
    re.IGNORECASE,
)
_MD_IMAGE_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_MD_LINK_PATTERN = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")
_HTML_ATTR_PATTERN = re.compile(
    r'(<(?:img|source|a|video)\b[^>]*\b(?:src|href)=["\'])([^"\']+)(["\'][^>]*>)',
    re.IGNORECASE,
)

_canonical_hex_error_code = canonical_hex_error_code
_expand_errorcode_search_variants = expand_errorcode_search_variants
_extract_error_codes_from_text = extract_hex_error_codes_from_text


def _file_extension(url: str) -> str:
    path = url.split("?")[0].split("#")[0].rstrip("/")
    if "." not in path:
        return ""
    return path.rsplit(".", 1)[-1].lower()


def _normalize_matched_url(url: str) -> str:
    return url.rstrip(".,;:!?)")


def _ensure_https_url(url: str) -> str:
    lowered = url.lower()
    if lowered.startswith(("http://", "https://")):
        return url
    if url.startswith("//"):
        return f"https:{url}"
    return f"https://{url.lstrip('/')}"


def _normalize_resource_url(url: str) -> str:
    return _ensure_https_url(_normalize_matched_url(url.strip()))


def _is_resource_url(url: str) -> bool:
    return _file_extension(url) in _RESOURCE_EXTENSIONS


def _format_resource_url(url: str) -> str:
    ext = _file_extension(url)
    if ext in _IMAGE_EXTENSIONS:
        return f"![{ext}]({url})"
    if ext in _VIDEO_EXTENSIONS:
        return (
            f'<video controls width="300" height="150">'
            f'<source src="{url}" type="video/{ext}"></video>'
        )
    return f"[{url}]({url})"


def _normalize_markdown_urls(content: str) -> str:
    def replace_image(match: re.Match[str]) -> str:
        alt, url = match.group(1), match.group(2)
        url = _normalize_resource_url(url)
        ext = _file_extension(url)
        if ext in _VIDEO_EXTENSIONS:
            return _format_resource_url(url)
        if ext in _FILE_EXTENSIONS:
            return f"[{alt or url}]({url})"
        return f"![{alt or ext or 'image'}]({url})"

    def replace_link(match: re.Match[str]) -> str:
        text, url = match.group(1), match.group(2)
        url = _normalize_resource_url(url)
        ext = _file_extension(url)
        if ext in _IMAGE_EXTENSIONS:
            return f"![{text or ext}]({url})"
        if ext in _VIDEO_EXTENSIONS:
            return _format_resource_url(url)
        if ext in _FILE_EXTENSIONS:
            return f"[{text or url}]({url})"
        return f"[{text}]({url})"

    content = _MD_IMAGE_PATTERN.sub(replace_image, content)
    return _MD_LINK_PATTERN.sub(replace_link, content)


def _normalize_html_urls(content: str) -> str:
    def replace_attr(match: re.Match[str]) -> str:
        prefix, url, suffix = match.group(1), match.group(2), match.group(3)
        return f"{prefix}{_normalize_resource_url(url)}{suffix}"

    return _HTML_ATTR_PATTERN.sub(replace_attr, content)


def _replace_urls_in_plain_text(text: str) -> str:
    if not text:
        return text

    matches: list[tuple[int, int, str]] = []
    for match in _URL_PATTERN.finditer(text):
        url = _normalize_resource_url(match.group(0))
        if _is_resource_url(url):
            matches.append((match.start(), match.end(), url))

    if not matches:
        return text

    parts: list[str] = []
    cursor = 0
    for start, end, url in matches:
        parts.append(text[cursor:start])
        parts.append(_format_resource_url(url))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def format_knowledge_content_urls(content: str) -> str:
    """将知识片段中的资源 URL 按类型格式化为 Markdown / HTML。"""
    if not content:
        return content

    content = _normalize_markdown_urls(content)
    content = _normalize_html_urls(content)
    segments = _FORMATTED_SEGMENT_PATTERN.split(content)
    return "".join(
        segment if index % 2 == 1 else _replace_urls_in_plain_text(segment)
        for index, segment in enumerate(segments)
    )


def format_knowledge_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    formatted: list[dict[str, Any]] = []
    for hit in hits:
        item = dict(hit)
        content = str(item.get("content") or "")
        if content:
            item["content"] = format_knowledge_content_urls(content)
        formatted.append(item)
    return formatted


@dataclass
class KnowledgeSearchFilters:
    device_type: int | None = None
    region: str = DEFAULT_SEARCH_REGION
    lang_code: str = "zh-CN"
    products: list[str] = field(default_factory=list)
    product_category: str | None = None
    matched_products: list[dict[str, Any]] = field(default_factory=list)
    errorcodes: list[str] = field(default_factory=list)
    parse_meta: dict[str, Any] = field(default_factory=dict)



def warmup_reranker(model_name: str | None = None) -> None:
    from chat.sop_vector.tei_client import check_rerank_service

    settings = load_sop_vector_settings()
    check_rerank_service(settings=settings)


def rerank_hits(
    query: str,
    hits: list[dict[str, Any]],
    *,
    top_k: int = RERANK_TOP_K,
    model_name: str | None = None,
) -> list[dict[str, Any]]:
    if not hits:
        return []
    if len(hits) <= top_k:
        return [dict(hit) for hit in hits]

    from chat.sop_vector.tei_client import rerank_texts

    settings = load_sop_vector_settings()
    _ = model_name or settings.reranker_model
    texts = [str(hit.get("content") or "") for hit in hits]
    ranked_items = rerank_texts(query, texts, settings=settings)

    results: list[dict[str, Any]] = []
    for item in ranked_items[:top_k]:
        index = int(item.get("index", 0))
        if index < 0 or index >= len(hits):
            continue
        hit = dict(hits[index])
        hit["rerank_score"] = round(float(item.get("score", 0)), 4)
        results.append(hit)
    return results


def _parse_device_type_from_text(text: str) -> int | None:
    lowered = (text or "").lower()
    if not lowered:
        return None
    for device_type, keywords in _DEVICE_KEYWORDS:
        for kw in keywords:
            if kw.lower() in lowered:
                return int(device_type)
    return None


def _device_type_from_category(category: str | None) -> int | None:
    if not category:
        return None
    return _CATEGORY_TO_DEVICE_TYPE.get(str(category).strip().lower())


def _catalog_item_by_name(catalog: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("product_name") or "").strip(): item
        for item in catalog
        if str(item.get("product_name") or "").strip()
    }


def _build_matched_product_details(
    canonical_names: list[str],
    catalog: list[dict[str, Any]],
    default_region: str,
) -> list[dict[str, Any]]:
    by_name = _catalog_item_by_name(catalog)
    details: list[dict[str, Any]] = []
    for name in canonical_names:
        item = by_name.get(name) or {}
        details.append(
            {
                "product_name": name,
                "product_category": item.get("product_category") or "sweeper_machine",
                "region": normalize_region(item.get("region")) or default_region,
            }
        )
    return details


def _normalize_hex_error_code(code: str) -> str | None:
    return _canonical_hex_error_code(code)


def _resolve_error_codes(
    query: str,
    context: dict[str, Any] | None = None,
    history_messages: list[dict[str, str]] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    context = context or {}
    meta: dict[str, Any] = {"source": None}
    texts = [query]

    for key in ("errorcode", "error_code", "errorcodes", "error_codes"):
        value = context.get(key)
        if isinstance(value, str) and value.strip():
            texts.append(value.strip())
        elif isinstance(value, list):
            texts.extend(str(item).strip() for item in value if str(item).strip())

    if history_messages:
        texts.extend(
            str(message.get("content") or "").strip()
            for message in history_messages[-4:]
            if str(message.get("content") or "").strip()
        )

    seen: set[str] = set()
    errorcodes: list[str] = []
    for text in texts:
        for code in _extract_error_codes_from_text(text):
            if code not in seen:
                seen.add(code)
                errorcodes.append(code)
        for part in re.split(r"[,，\s]+", text):
            normalized = _normalize_hex_error_code(part)
            if normalized and normalized not in seen:
                seen.add(normalized)
                errorcodes.append(normalized)

    if errorcodes:
        if _extract_error_codes_from_text(query):
            meta["source"] = "query"
        else:
            meta["source"] = "context_or_history"
    meta["errorcodes"] = errorcodes
    return errorcodes, meta


def _scan_products_in_text(query: str, catalog: list[dict[str, Any]]) -> list[str]:
    text = (query or "").strip()
    text_lower = text.lower()
    if not text or not catalog:
        return []

    candidates: list[tuple[int, str]] = []
    for item in catalog:
        canonical = str(item.get("product_name") or "").strip()
        if not canonical:
            continue
        tokens = [canonical, *(str(a).strip() for a in (item.get("aliases") or []) if str(a).strip())]
        for token in tokens:
            if len(token) < 2:
                continue
            if token in text or token.lower() in text_lower:
                candidates.append((len(token), canonical))
    if not candidates:
        return []

    candidates.sort(key=lambda x: x[0], reverse=True)
    seen: set[str] = set()
    result: list[str] = []
    for _, canonical in candidates:
        if canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result[:3]


def _expand_product_filter_names(
    names: list[str],
    catalog: list[dict[str, Any]],
    region: str,
) -> list[str]:
    if not names:
        return []

    alias_map = build_alias_map(catalog)
    canonical_names = canonicalize_product_names(names, alias_map)
    expanded: set[str] = set()
    for name in names + canonical_names:
        text = str(name).strip()
        if text:
            expanded.add(text)

    canonical_set = set(canonical_names)
    for item in catalog:
        product_name = str(item.get("product_name") or "").strip()
        if product_name not in canonical_set:
            continue
        expanded.add(product_name)
        for alias in item.get("aliases") or []:
            alias_text = str(alias).strip()
            if alias_text:
                expanded.add(alias_text)

    mapping = get_product_mapping(region or "cn")
    for key, value in mapping.items():
        key_text = str(key).strip()
        value_text = str(value).strip()
        if canonical_set & {key_text, value_text}:
            if key_text:
                expanded.add(key_text)
            if value_text:
                expanded.add(value_text)

    return sorted(expanded)


async def resolve_product_filters(
    query: str,
    *,
    region: str = DEFAULT_SEARCH_REGION,
    history_messages: list[dict[str, str]] | None = None,
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    """先拉取产品目录（同 list_product_catalog），再从用户问题中识别型号。"""
    norm_region = normalize_region(region) or DEFAULT_SEARCH_REGION
    meta: dict[str, Any] = {
        "source": None,
        "region": norm_region,
        "catalog_count": 0,
        "catalog": [],
    }
    catalog = await list_product_catalog()
    meta["catalog_count"] = len(catalog)

    if not catalog:
        meta["matched_products"] = []
        meta["products"] = []
        return [], [], meta

    alias_map = build_alias_map(catalog)
    canonical_names: list[str] = []
    scanned = _scan_products_in_text(query, catalog)
    if scanned:
        canonical_names = canonicalize_product_names(scanned, alias_map)
        meta["source"] = "query_scan"

    if not canonical_names:
        select_question = query
        if history_messages:
            recent = [
                str(m.get("content") or "").strip()
                for m in history_messages[-4:]
                if str(m.get("content") or "").strip()
            ]
            if recent:
                select_question = "\n".join(recent + [query])
        select_meta = await select_products_by_llm(
            select_question, catalog, norm_region, max_products=1
        )
        llm_products = select_meta.get("products") or []
        if llm_products:
            canonical_names = canonicalize_product_names(llm_products, alias_map)
            meta["source"] = "llm"
            meta["select"] = select_meta

    matched_products = _build_matched_product_details(
        canonical_names, catalog, norm_region
    )
    filter_region = norm_region
    if matched_products:
        filter_region = matched_products[0].get("region") or norm_region

    products = _expand_product_filter_names(canonical_names, catalog, filter_region)
    meta["matched_products"] = matched_products
    meta["products"] = products
    meta["filter_region"] = filter_region
    return products, matched_products, meta


async def parse_knowledge_filters(
    query: str,
    context: dict[str, Any] | None = None,
    history_messages: list[dict[str, str]] | None = None,
) -> KnowledgeSearchFilters:
    context = context or {}
    lang_code = get_locale_from_context(context)

    products, matched_products, product_meta = await resolve_product_filters(
        query,
        region=DEFAULT_SEARCH_REGION,
        history_messages=history_messages,
    )
    errorcodes, error_meta = _resolve_error_codes(
        query,
        context=context,
        history_messages=history_messages,
    )

    product_category = None
    device_type = None
    region = DEFAULT_SEARCH_REGION
    if matched_products:
        product_category = matched_products[0].get("product_category")
        device_type = _device_type_from_category(product_category)
        region = normalize_region(matched_products[0].get("region")) or DEFAULT_SEARCH_REGION
    if device_type is None:
        device_type = _parse_device_type_from_text(query)

    return KnowledgeSearchFilters(
        device_type=device_type,
        region=region,
        lang_code=lang_code,
        products=products,
        product_category=product_category,
        matched_products=matched_products,
        errorcodes=errorcodes,
        parse_meta={"product": product_meta, "errorcode": error_meta},
    )


async def summarize_knowledge_hits(
    query: str,
    hits: list[dict[str, Any]],
    *,
    lang_code: str = "zh-CN",
) -> str:
    if not hits:
        return "未在知识库中检索到相关排障内容，请补充产品型号或更具体的故障现象后重试。"

    chunks: list[str] = []
    for idx, hit in enumerate(hits, start=1):
        title = str(hit.get("title") or "").strip() or f"片段{idx}"
        content = str(hit.get("content") or "").strip()
        score = hit.get("rerank_score", hit.get("score"))
        chunks.append(f"【{idx}】{title}（相关度 {score}）\n{content}")
    knowledge_text = "\n\n---\n\n".join(chunks)

    client = get_deepseek_client()
    response = await client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": KNOWLEDGE_SUMMARY_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"回复语言：{lang_code}\n"
                    f"用户问题：{query}\n\n"
                    f"检索知识片段：\n{knowledge_text}"
                ),
            },
        ],
        temperature=0.3,
        stream=False,
    )
    return (response.choices[0].message.content or "").strip()


def _vector_search(
    query: str,
    filters: KnowledgeSearchFilters,
    *,
    top_k: int,
    with_products: bool = True,
) -> tuple[list[dict[str, Any]], str]:
    index = SopVectorIndex()
    errorcodes = (
        _expand_errorcode_search_variants(filters.errorcodes)
        if filters.errorcodes
        else None
    )

    if errorcodes:
        search_query = query or " ".join(errorcodes)
        hits = index.search(
            search_query,
            top_k=top_k,
            region=filters.region,
            lang_code=filters.lang_code,
            device_type=filters.device_type,
            products=filters.products if with_products else None,
            errorcodes=errorcodes,
        )
        if hits:
            return hits, "errorcode"

        hits = index.search(
            search_query,
            top_k=top_k,
            region=filters.region,
            lang_code=filters.lang_code,
            device_type=filters.device_type,
            products=None,
            errorcodes=errorcodes,
        )
        if hits:
            return hits, "errorcode_without_product"

        hits = index.search(
            search_query,
            top_k=top_k,
            region=filters.region,
            lang_code=filters.lang_code,
            device_type=None,
            products=None,
            errorcodes=errorcodes,
        )
        if hits:
            return hits, "errorcode_relaxed"

        hits = index.search(
            search_query,
            top_k=top_k,
            region=None,
            lang_code=filters.lang_code,
            device_type=None,
            products=None,
            errorcodes=errorcodes,
        )
        if hits:
            return hits, "errorcode_only"

    products = filters.products if with_products else None
    hits = index.search(
        query,
        top_k=top_k,
        region=filters.region,
        lang_code=filters.lang_code,
        device_type=filters.device_type,
        products=products or None,
    )
    scope = "full"
    if not hits and with_products and filters.products:
        hits = index.search(
            query,
            top_k=top_k,
            region=filters.region,
            lang_code=filters.lang_code,
            device_type=filters.device_type,
            products=None,
        )
        scope = "without_product"
    if not hits and filters.device_type is not None:
        hits = index.search(
            query,
            top_k=top_k,
            region=filters.region,
            lang_code=filters.lang_code,
            device_type=None,
            products=None,
        )
        scope = "without_device_type"
    return hits, scope


async def search_knowledge(
    query: str,
    *,
    top_k: int | None = None,
    context: dict[str, Any] | None = None,
    history_messages: list[dict[str, str]] | None = None,
    summarize: bool = True,
) -> list[dict[str, Any]]:
    """兼容旧接口：返回重排后的 hits（不含 summary 字段）。"""
    result = await search_knowledge_with_summary(
        query,
        top_k=top_k,
        context=context,
        history_messages=history_messages,
        summarize=summarize,
    )
    return result.get("hits") or []


async def search_knowledge_with_summary(
    query: str,
    *,
    top_k: int | None = None,
    context: dict[str, Any] | None = None,
    history_messages: list[dict[str, str]] | None = None,
    summarize: bool = True,
) -> dict[str, Any]:
    query = (query or "").strip()
    if not query:
        return {
            "query": query,
            "filters": {},
            "hits": [],
            "hit_count": 0,
            "summary": "",
            "recall_count": 0,
        }

    settings = load_sop_vector_settings()
    recall_top_k = settings.vector_recall_top_k
    rerank_top_k = top_k or settings.rerank_top_k

    context = context or {}
    history_messages = history_messages or context.get("history_messages")
    filters = await parse_knowledge_filters(query, context, history_messages=history_messages)
    logger.info(f"知识库检索解析条件 query={query} filters={filters}")
    recalled, search_scope = _vector_search(query, filters, top_k=recall_top_k)
    reranked = format_knowledge_hits(rerank_hits(query, recalled, top_k=rerank_top_k))
    logger.info(f"知识库检索结果 query={query} recalled={recalled} reranked={reranked}")
    summary = ""
    if summarize and reranked:
        try:
            summary = await summarize_knowledge_hits(
                query, reranked, lang_code=filters.lang_code
            )
        except Exception as exc:
            logger.exception(f"知识库 LLM 总结失败: {exc}")
            summary = "\n\n".join(
                f"{hit.get('title', '')}: {str(hit.get('content') or '')[:300]}"
                for hit in reranked
            )

    filter_payload = {
        "device_type": filters.device_type,
        "region": filters.region,
        "lang_code": filters.lang_code,
        "product_category": filters.product_category,
        "matched_products": filters.matched_products,
        "products": filters.products,
        "errorcodes": filters.errorcodes,
        "search_scope": search_scope,
        **filters.parse_meta,
    }

    return {
        "query": query,
        "filters": filter_payload,
        "recall_count": len(recalled),
        "hits": reranked,
        "hit_count": len(reranked),
        "summary": summary,
    }
