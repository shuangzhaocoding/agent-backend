# -*- coding: utf-8 -*-
#
# 将 sop_v3 表批量导入 Qdrant 向量库
# 用法：cd app && python -m chat.sop_vector.batch_import [--rebuild] [--dry-run] [--device-type 1]
# 不传 --device-type 时导入全部机型
#
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from typing import Any

from loguru import logger
from tortoise import Tortoise

from chat.sop_vector.chunking import build_chunked_documents
from chat.sop_vector.config import load_sop_vector_settings
from chat.sop_vector.embedder import encode_passages
from chat.sop_vector.vector_index import SopVectorIndex, build_sop_metadata
from connections.tortoise_mysql import SECOND_ORM_LINK_CONF
from models.fae_service_db_model import SopToErrorCode, SopToProduct, SopV3Model


async def _load_relation_maps(master_ids: list[int]) -> tuple[dict[int, list[str]], dict[int, list[str]]]:
    products_map: dict[int, list[str]] = defaultdict(list)
    errorcodes_map: dict[int, list[str]] = defaultdict(list)
    if not master_ids:
        return products_map, errorcodes_map

    product_rows = await SopToProduct.filter(sop_master_id__in=master_ids).values(
        "sop_master_id", "product"
    )
    for row in product_rows:
        products_map[row["sop_master_id"]].append(row["product"])

    error_rows = await SopToErrorCode.filter(sop_master_id__in=master_ids).values(
        "sop_master_id", "errorcode"
    )
    for row in error_rows:
        errorcodes_map[row["sop_master_id"]].append(row["errorcode"])
    return products_map, errorcodes_map


async def fetch_active_sops(
    *,
    device_type: int | None = None,
    region: str | None = None,
    lang_code: str | None = None,
    limit: int | None = None,
) -> list[SopV3Model]:
    query = SopV3Model.filter(is_actived=True)
    if device_type is not None:
        query = query.filter(device_type=device_type)
    if region:
        query = query.filter(region=region)
    if lang_code:
        query = query.filter(lang_code=lang_code)
    query = query.order_by("id")
    if limit:
        query = query.limit(limit)
    return await query.all()


async def import_sop_vectors(
    *,
    rebuild: bool = False,
    dry_run: bool = False,
    device_type: int | None = None,
    region: str | None = None,
    lang_code: str | None = None,
    limit: int | None = None,
    show_embed_progress: bool = True,
) -> dict[str, Any]:
    settings = load_sop_vector_settings()
    index = SopVectorIndex(settings)

    if not dry_run:
        index.ensure_collection(recreate=rebuild)

    sops = await fetch_active_sops(
        device_type=device_type,
        region=region,
        lang_code=lang_code,
        limit=limit,
    )
    master_ids = sorted({sop.master_id for sop in sops if sop.master_id})
    products_map, errorcodes_map = await _load_relation_maps(master_ids)

    stats = {
        "total_sops": len(sops),
        "imported_sops": 0,
        "skipped_empty": 0,
        "total_chunks": 0,
        "total_points": 0,
        "device_type": device_type,
        "dry_run": dry_run,
        "rebuild": rebuild,
    }

    for sop in sops:
        master_id = sop.master_id or sop.id
        chunk_texts = build_chunked_documents(
            title=sop.title,
            key_word=sop.key_word,
            description_html=sop.description or "",
            cs_content_html=sop.cs_content or "",
            chunk_size=settings.chunk_size,
            overlap=settings.chunk_overlap,
        )
        if not chunk_texts:
            stats["skipped_empty"] += 1
            logger.warning(f"跳过空内容 SOP id={sop.id} title={sop.title!r}")
            continue

        metadata = build_sop_metadata(
            sop_id=sop.id,
            title=sop.title,
            created_at=sop.created_at,
            modified_at=sop.modified_at,
            device_type=int(sop.device_type),
            region=sop.region,
            lang_code=str(sop.lang_code.value if hasattr(sop.lang_code, "value") else sop.lang_code),
            created_by=sop.created_by,
            is_actived=bool(sop.is_actived),
            master_id=master_id,
            products=products_map.get(master_id, []),
            errorcodes=errorcodes_map.get(master_id, []),
        )

        stats["imported_sops"] += 1
        stats["total_chunks"] += len(chunk_texts)

        if dry_run:
            logger.info(
                f"[dry-run] sop_id={sop.id} chunks={len(chunk_texts)} "
                f"title={sop.title[:40]!r}"
            )
            continue

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
            replace_existing=not rebuild,
        )
        stats["total_points"] += point_count

    if show_embed_progress and not dry_run:
        logger.info(
            "导入完成: sops={imported_sops}/{total_sops}, "
            "chunks={total_chunks}, points={total_points}, skipped={skipped_empty}".format(**stats)
        )
    return stats


async def _run(args: argparse.Namespace) -> None:
    await Tortoise.init(config=SECOND_ORM_LINK_CONF)
    try:
        stats = await import_sop_vectors(
            rebuild=args.rebuild,
            dry_run=args.dry_run,
            device_type=args.device_type,
            region=(args.region or "").strip() or None,
            lang_code=(args.lang_code or "").strip() or None,
            limit=args.limit,
        )
        print(stats)
    finally:
        await Tortoise.close_connections()


def main() -> None:
    parser = argparse.ArgumentParser(description="批量导入 sop_v3 到 Qdrant 向量库")
    parser.add_argument("--rebuild", action="store_true", help="删除并重建 collection 后全量导入")
    parser.add_argument("--dry-run", action="store_true", help="仅统计与分块，不写向量库")
    parser.add_argument(
        "--device-type",
        type=int,
        default=None,
        help="设备类型：1扫地机 2洗地机 3吸尘器 4除螨仪 9其他；不传则导入全部机型",
    )
    parser.add_argument("--region", default="", help="仅导入指定 region，如 cn / ovs")
    parser.add_argument("--lang-code", default="", help="仅导入指定语言，如 zh-CN")
    parser.add_argument("--limit", type=int, default=0, help="限制导入条数（调试用）")
    args = parser.parse_args()
    if args.limit <= 0:
        args.limit = None
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
