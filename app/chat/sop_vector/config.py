# -*- coding: utf-8 -*-
#
# SOP 向量化与 Qdrant 配置
#
from dataclasses import dataclass

from common import config_file


@dataclass(frozen=True)
class SopVectorSettings:
    qdrant_host: str
    qdrant_port: int
    qdrant_https: bool
    qdrant_collection: str
    model_name: str
    vector_size: int
    chunk_size: int
    chunk_overlap: int
    embed_batch_size: int
    upsert_batch_size: int
    reranker_model: str
    embedding_service_url: str
    rerank_service_url: str
    vector_recall_top_k: int
    rerank_top_k: int


def load_sop_vector_settings() -> SopVectorSettings:
    conf = config_file.read_conf(config_file.config_dir) or {}
    qdrant = conf.get("qdrant") or {}
    sop_vector = conf.get("sop_vector") or {}
    return SopVectorSettings(
        qdrant_host=str(qdrant.get("host", "127.0.0.1")),
        qdrant_port=int(qdrant.get("port", 6333)),
        qdrant_https=bool(qdrant.get("https", False)),
        qdrant_collection=str(qdrant.get("collection", "fae_sop")),
        model_name=str(sop_vector.get("model_name", "BAAI/bge-small-zh-v1.5")),
        vector_size=int(sop_vector.get("vector_size", 512)),
        chunk_size=int(sop_vector.get("chunk_size", 500)),
        chunk_overlap=int(sop_vector.get("chunk_overlap", 100)),
        embed_batch_size=int(sop_vector.get("embed_batch_size", 32)),
        upsert_batch_size=int(sop_vector.get("upsert_batch_size", 200)),
        reranker_model=str(sop_vector.get("reranker_model", "BAAI/bge-reranker-base")),
        embedding_service_url=str(
            sop_vector.get("embedding_service_url", "http://127.0.0.1:8081")
        ),
        rerank_service_url=str(
            sop_vector.get("rerank_service_url", "http://127.0.0.1:8082")
        ),
        vector_recall_top_k=int(sop_vector.get("vector_recall_top_k", 10)),
        rerank_top_k=int(sop_vector.get("rerank_top_k", 3)),
    )
