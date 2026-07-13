# -*- coding: utf-8 -*-
#
# 单条 / 批量 SOP 向量重索引（更新时先删旧向量再写入新向量）
#
from __future__ import annotations

from typing import Any

from common.logger import logger

from chat.sop_vector.batch_import import _load_relation_maps
from chat.sop_vector.chunking import build_chunked_documents
from chat.sop_vector.config import load_sop_vector_settings
from chat.sop_vector.embedder import encode_passages
from chat.sop_vector.vector_index import SopVectorIndex, build_sop_metadata
from models.fae_service_db_model import SopV3Model


async def reindex_master_sop_vectors(master_id: int) -> dict[str, Any]:
    """删除 master_id 下失效 SOP 的旧向量，并重建所有生效 SOP 的向量。"""
    settings = load_sop_vector_settings()
    index = SopVectorIndex(settings)
    index.ensure_collection()

    all_sops = await SopV3Model.filter(master_id=master_id).all()
    active_sops = [sop for sop in all_sops if sop.is_actived]
    inactive_ids = [sop.id for sop in all_sops if not sop.is_actived]

    deleted_inactive = 0
    for sop_id in inactive_ids:
        index.delete_by_sop_id(sop_id)
        deleted_inactive += 1

    products_map, errorcodes_map = await _load_relation_maps([master_id])
    products = products_map.get(master_id, [])
    errorcodes = errorcodes_map.get(master_id, [])

    stats: dict[str, Any] = {
        "master_id": master_id,
        "deleted_inactive": deleted_inactive,
        "reindexed": 0,
        "skipped_empty": 0,
        "total_points": 0,
    }

    for sop in active_sops:
        chunk_texts = build_chunked_documents(
            title=sop.title,
            key_word=sop.key_word,
            description_html=sop.description or "",
            cs_content_html=sop.cs_content or "",
            chunk_size=settings.chunk_size,
            overlap=settings.chunk_overlap,
        )
        if not chunk_texts:
            index.delete_by_sop_id(sop.id)
            stats["skipped_empty"] += 1
            logger.warning(f"SOP 向量为空已清理 sop_id={sop.id} master_id={master_id}")
            continue

        metadata = build_sop_metadata(
            sop_id=sop.id,
            title=sop.title,
            created_at=sop.created_at,
            modified_at=sop.modified_at,
            device_type=int(sop.device_type),
            region=sop.region,
            lang_code=str(
                sop.lang_code.value if hasattr(sop.lang_code, "value") else sop.lang_code
            ),
            created_by=sop.created_by,
            is_actived=bool(sop.is_actived),
            master_id=master_id,
            products=products,
            errorcodes=errorcodes,
        )
        vectors = encode_passages(
            chunk_texts,
            settings=settings,
            show_progress=False,
        )
        point_count = index.upsert_sop_chunks(
            sop_id=sop.id,
            chunk_texts=chunk_texts,
            metadata=metadata,
            vectors=vectors,
            replace_existing=True,
        )
        stats["reindexed"] += 1
        stats["total_points"] += point_count

    logger.info(
        "SOP 向量重索引完成 master_id={master_id} reindexed={reindexed} "
        "points={total_points} deleted_inactive={deleted_inactive} skipped={skipped_empty}".format(
            **stats
        )
    )
    return stats


async def schedule_sop_vector_reindex(master_id: int) -> str | None:
    """投递 Taskiq 任务：异步重索引指定 master_id 的 SOP 向量。"""
    from async_task_module.dispatch import kiq_task
    from async_task_module.tasks.sop_vector_tasks import reindex_sop_vectors_task

    try:
        celery_id = (await kiq_task(reindex_sop_vectors_task, int(master_id))).task_id
        logger.info(f"已投递 SOP 向量重索引 master_id={master_id} celery_task_id={celery_id}")
        return celery_id
    except Exception as exc:
        logger.error(f"投递 SOP 向量重索引失败 master_id={master_id}: {exc}")
        return None
