# -*- coding: utf-8 -*-
#
# 用户停止生成：Redis 标记 + Worker 进程内缓存
#
from __future__ import annotations

from typing import Any

from chat.redis_generation import (
    clear_generation_cancel_requested,
    is_generation_cancel_requested,
    request_generation_cancel as redis_request_generation_cancel,
)

_local_cancelled: set[str] = set()


def generation_id_from_context(context: dict[str, Any] | None) -> str | None:
    gid = (context or {}).get("_generation_id")
    if not gid:
        return None
    return str(gid)


def mark_generation_cancelled_local(generation_id: str) -> None:
    _local_cancelled.add(generation_id)


def clear_generation_cancelled_local(generation_id: str) -> None:
    _local_cancelled.discard(generation_id)


async def is_generation_cancelled(generation_id: str) -> bool:
    if generation_id in _local_cancelled:
        return True
    if await is_generation_cancel_requested(generation_id):
        mark_generation_cancelled_local(generation_id)
        return True
    return False


async def request_generation_cancel(generation_id: str) -> None:
    mark_generation_cancelled_local(generation_id)
    await redis_request_generation_cancel(generation_id)


async def clear_generation_cancel_state(generation_id: str) -> None:
    clear_generation_cancelled_local(generation_id)
    await clear_generation_cancel_requested(generation_id)


def inject_generation_id_into_payload_context(
    payload_data: dict[str, Any],
    generation_id: str,
) -> dict[str, Any]:
    context = dict(payload_data.get("context") or {})
    context["_generation_id"] = generation_id
    payload_data = dict(payload_data)
    payload_data["context"] = context
    return payload_data
