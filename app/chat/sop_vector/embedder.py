# -*- coding: utf-8 -*-
#
# SOP 向量化（通过 TEI HTTP 服务）
#
from __future__ import annotations

from chat.sop_vector.config import SopVectorSettings, load_sop_vector_settings
from chat.sop_vector.tei_client import embed_text, embed_texts


def encode_passages(
    texts: list[str],
    *,
    settings: SopVectorSettings | None = None,
    batch_size: int | None = None,
    show_progress: bool = False,
) -> list[list[float]]:
    if not texts:
        return []
    settings = settings or load_sop_vector_settings()
    batch_size = batch_size or settings.embed_batch_size
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        vectors.extend(embed_texts(batch, settings=settings))
        if show_progress:
            done = min(start + batch_size, len(texts))
            print(f"TEI embed 进度 {done}/{len(texts)}")
    return vectors


def encode_query(query: str, *, settings: SopVectorSettings | None = None) -> list[float]:
    settings = settings or load_sop_vector_settings()
    return embed_text(f"query: {(query or '').strip()}", settings=settings)
