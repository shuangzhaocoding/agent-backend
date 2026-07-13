# -*- coding: utf-8 -*-
#
# Qdrant 向量索引：collection 管理、写入与检索
#
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PointStruct,
    VectorParams,
)

from chat.sop_vector.config import SopVectorSettings, load_sop_vector_settings
from chat.sop_vector.embedder import encode_passages, encode_query

POINT_ID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def make_point_id(sop_id: int, chunk_index: int) -> str:
    return str(uuid.uuid5(POINT_ID_NAMESPACE, f"{sop_id}:{chunk_index}"))


def _dt_to_str(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.strftime("%Y-%m-%d %H:%M:%S")


class SopVectorIndex:
    def __init__(self, settings: SopVectorSettings | None = None):
        self.settings = settings or load_sop_vector_settings()
        self.client = QdrantClient(
            host=self.settings.qdrant_host,
            port=self.settings.qdrant_port,
            https=self.settings.qdrant_https,
            check_compatibility=False,
        )

    @property
    def collection_name(self) -> str:
        return self.settings.qdrant_collection

    def ensure_collection(self, *, recreate: bool = False) -> None:
        exists = self.client.collection_exists(self.collection_name)
        if recreate and exists:
            self.client.delete_collection(self.collection_name)
            exists = False
        if exists:
            return
        self.client.create_collection(
            collection_name=self.collection_name,
            vectors_config=VectorParams(
                size=self.settings.vector_size,
                distance=Distance.COSINE,
            ),
        )
        self._ensure_payload_indexes()

    def _ensure_payload_indexes(self) -> None:
        for field_name in ("region", "lang_code", "device_type", "master_id", "id"):
            try:
                self.client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name=field_name,
                    field_schema="keyword" if field_name != "device_type" else "integer",
                )
            except Exception:
                # 索引已存在或当前 Qdrant 版本不支持时忽略
                pass

    def delete_by_sop_id(self, sop_id: int) -> None:
        if not self.client.collection_exists(self.collection_name):
            return
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=Filter(
                must=[FieldCondition(key="id", match=MatchValue(value=sop_id))]
            ),
        )

    def upsert_points(self, points: list[PointStruct]) -> None:
        if not points:
            return
        batch_size = self.settings.upsert_batch_size
        for start in range(0, len(points), batch_size):
            batch = points[start : start + batch_size]
            self.client.upsert(collection_name=self.collection_name, points=batch)

    def build_points(
        self,
        *,
        sop_id: int,
        chunk_texts: list[str],
        metadata: dict[str, Any],
        vectors: list[list[float]] | None = None,
    ) -> list[PointStruct]:
        if not chunk_texts:
            return []
        if vectors is None:
            vectors = encode_passages(
                chunk_texts,
                settings=self.settings,
                show_progress=False,
            )
        if len(vectors) != len(chunk_texts):
            raise ValueError("向量数量与分块数量不一致")

        chunk_total = len(chunk_texts)
        points: list[PointStruct] = []
        for idx, (text, vector) in enumerate(zip(chunk_texts, vectors)):
            payload = {
                **metadata,
                "chunk_index": idx,
                "chunk_total": chunk_total,
                "text": text,
            }
            points.append(
                PointStruct(
                    id=make_point_id(sop_id, idx),
                    vector=vector,
                    payload=payload,
                )
            )
        return points

    def upsert_sop_chunks(
        self,
        *,
        sop_id: int,
        chunk_texts: list[str],
        metadata: dict[str, Any],
        vectors: list[list[float]] | None = None,
        replace_existing: bool = True,
    ) -> int:
        if replace_existing:
            self.delete_by_sop_id(sop_id)
        points = self.build_points(
            sop_id=sop_id,
            chunk_texts=chunk_texts,
            metadata=metadata,
            vectors=vectors,
        )
        self.upsert_points(points)
        return len(points)

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        region: str | None = None,
        lang_code: str | None = None,
        device_type: int | None = None,
        products: list[str] | None = None,
        errorcodes: list[str] | None = None,
        score_threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        if not query or not query.strip():
            return []
        if not self.client.collection_exists(self.collection_name):
            return []

        must: list[FieldCondition] = [
            FieldCondition(key="is_actived", match=MatchValue(value=True)),
        ]
        if region:
            must.append(FieldCondition(key="region", match=MatchValue(value=region)))
        if lang_code:
            must.append(FieldCondition(key="lang_code", match=MatchValue(value=lang_code)))
        if device_type is not None:
            must.append(FieldCondition(key="device_type", match=MatchValue(value=device_type)))
        if products:
            must.append(FieldCondition(key="products", match=MatchAny(any=products)))
        if errorcodes:
            must.append(FieldCondition(key="errorcodes", match=MatchAny(any=errorcodes)))

        query_vector = encode_query(query, settings=self.settings)
        response = self.client.query_points(
            collection_name=self.collection_name,
            query=query_vector,
            query_filter=Filter(must=must) if must else None,
            limit=top_k,
            score_threshold=score_threshold,
            with_payload=True,
            with_vectors=False,
        )
        results: list[dict[str, Any]] = []
        for hit in response.points or []:
            payload = hit.payload or {}
            results.append(
                {
                    "id": payload.get("id"),
                    "title": payload.get("title"),
                    "content": payload.get("text"),
                    "score": round(float(hit.score), 4),
                    "master_id": payload.get("master_id"),
                    "region": payload.get("region"),
                    "lang_code": payload.get("lang_code"),
                    "device_type": payload.get("device_type"),
                    "products": payload.get("products") or [],
                    "errorcodes": payload.get("errorcodes") or [],
                    "chunk_index": payload.get("chunk_index"),
                    "chunk_total": payload.get("chunk_total"),
                }
            )
        return results


def build_sop_metadata(
    *,
    sop_id: int,
    title: str,
    created_at: datetime | None,
    modified_at: datetime | None,
    device_type: int,
    region: str | None,
    lang_code: str,
    created_by: str | None,
    is_actived: bool,
    master_id: int | None = None,
    products: list[str] | None = None,
    errorcodes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": sop_id,
        "title": title,
        "created_at": _dt_to_str(created_at),
        "modified_at": _dt_to_str(modified_at),
        "device_type": int(device_type),
        "region": region or "",
        "lang_code": lang_code,
        "created_by": created_by or "",
        "is_actived": bool(is_actived),
        "master_id": master_id,
        "products": products or [],
        "errorcodes": errorcodes or [],
    }
