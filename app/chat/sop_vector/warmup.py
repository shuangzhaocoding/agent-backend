# -*- coding: utf-8 -*-
#
# SOP 向量 / 重排 TEI 服务预热（Worker 启动时探测 HTTP 可用性）
#
from __future__ import annotations

from common.logger import logger
from chat.sop_vector.config import load_sop_vector_settings
from chat.sop_vector.tei_client import check_embed_service, check_rerank_service


def warmup_sop_vector_models() -> None:
    settings = load_sop_vector_settings()
    logger.info(
        f"开始探测 TEI 服务 embed={settings.embedding_service_url} "
        f"rerank={settings.rerank_service_url}"
    )
    check_embed_service(settings=settings)
    check_rerank_service(settings=settings)
    logger.info("TEI 向量 / rerank 服务探测完成")
