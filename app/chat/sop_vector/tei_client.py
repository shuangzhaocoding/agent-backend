# -*- coding: utf-8 -*-
#
# HuggingFace TEI 服务 HTTP 客户端（embedding / rerank）
#
from __future__ import annotations

from typing import Any

import requests

from chat.sop_vector.config import SopVectorSettings, load_sop_vector_settings
from common.logger import logger

_DEFAULT_TIMEOUT = 120


def _base_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _embed_url(settings: SopVectorSettings | None = None) -> str:
    settings = settings or load_sop_vector_settings()
    return f"{_base_url(settings.embedding_service_url)}/embed"


def _rerank_url(settings: SopVectorSettings | None = None) -> str:
    settings = settings or load_sop_vector_settings()
    return f"{_base_url(settings.rerank_service_url)}/rerank"


def _parse_embed_response(data: Any, expected: int) -> list[list[float]]:
    if not isinstance(data, list) or not data:
        raise ValueError(f"TEI embed 返回为空: {data!r}")
    if expected == 1 and isinstance(data[0], (int, float)):
        return [data]
    if not isinstance(data[0], list):
        raise ValueError(f"TEI embed 返回格式异常: {type(data[0])}")
    if len(data) != expected:
        raise ValueError(f"TEI embed 数量不一致: expected={expected} got={len(data)}")
    return data


def embed_texts(
    texts: list[str],
    *,
    settings: SopVectorSettings | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> list[list[float]]:
    if not texts:
        return []
    settings = settings or load_sop_vector_settings()
    url = _embed_url(settings)
    response = requests.post(url, json={"inputs": texts}, timeout=timeout)
    response.raise_for_status()
    return _parse_embed_response(response.json(), len(texts))


def embed_text(
    text: str,
    *,
    settings: SopVectorSettings | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> list[float]:
    vectors = embed_texts([text], settings=settings, timeout=timeout)
    return vectors[0]


def rerank_texts(
    query: str,
    texts: list[str],
    *,
    settings: SopVectorSettings | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    if not texts:
        return []
    settings = settings or load_sop_vector_settings()
    url = _rerank_url(settings)
    response = requests.post(
        url,
        json={"query": query, "texts": texts, "raw_scores": False},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError(f"TEI rerank 返回格式异常: {data!r}")
    return data


def check_embed_service(*, settings: SopVectorSettings | None = None) -> None:
    embed_text("query: warmup", settings=settings, timeout=30)
    logger.info(f"TEI embedding 服务可用 url={_embed_url(settings)}")


def check_rerank_service(*, settings: SopVectorSettings | None = None) -> None:
    rerank_texts("warmup", ["warmup"], settings=settings, timeout=30)
    logger.info(f"TEI rerank 服务可用 url={_rerank_url(settings)}")
