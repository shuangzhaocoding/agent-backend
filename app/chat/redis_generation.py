# -*- coding: utf-8 -*-
#
# 生成任务 Redis 持久化（chunk 列表 + meta 哈希）
#
from __future__ import annotations

import json
from typing import Any

from chat.redis_client import get_chat_redis_settings, get_redis

GENERATION_STATUS_RUNNING = "running"
GENERATION_STATUS_PAUSED = "paused"
GENERATION_STATUS_COMPLETED = "completed"
GENERATION_STATUS_FAILED = "failed"
GENERATION_STATUS_CANCELLED = "cancelled"


def _prefix() -> str:
    return get_chat_redis_settings()["key_prefix"]


def _gen_key(generation_id: str) -> str:
    return f"{_prefix()}:{generation_id}"


def _chunks_key(generation_id: str) -> str:
    return f"{_gen_key(generation_id)}:chunks"


def _meta_key(generation_id: str) -> str:
    return f"{_gen_key(generation_id)}:meta"


def _signal_key(generation_id: str) -> str:
    return f"{_gen_key(generation_id)}:signal"


def _session_running_key(creator: str, session_id: str) -> str:
    return f"{_prefix()}:session_running:{creator}:{session_id}"


def _ttl() -> int:
    return int(get_chat_redis_settings()["ttl_seconds"])


def _payload_key(generation_id: str) -> str:
    return f"{_gen_key(generation_id)}:payload"


def _checkpoint_key(generation_id: str) -> str:
    return f"{_gen_key(generation_id)}:checkpoint"


def _cancel_key(generation_id: str) -> str:
    return f"{_gen_key(generation_id)}:cancel_requested"


async def _expire_keys(generation_id: str) -> None:
    redis = get_redis()
    ttl = _ttl()
    await redis.expire(_chunks_key(generation_id), ttl)
    await redis.expire(_meta_key(generation_id), ttl)
    await redis.expire(_signal_key(generation_id), ttl)
    await redis.expire(_payload_key(generation_id), ttl)
    await redis.expire(_checkpoint_key(generation_id), ttl)
    await redis.expire(_cancel_key(generation_id), ttl)


def meta_to_dict(raw: dict[str, str]) -> dict[str, Any]:
    if not raw:
        return {}
    result: dict[str, Any] = dict(raw)
    for field in ("workflow_data", "steps", "blocks"):
        text = raw.get(field)
        if text:
            try:
                result[field] = json.loads(text)
            except json.JSONDecodeError:
                result[field] = {} if field == "workflow_data" else []
        else:
            result[field] = {} if field == "workflow_data" else []
    if "offset" in result:
        result["offset"] = int(result.get("offset") or 0)
    return result


async def create_generation_meta(meta: dict[str, Any]) -> None:
    redis = get_redis()
    generation_id = meta["generation_id"]
    payload: dict[str, str] = {}
    for key, value in meta.items():
        if value is None:
            continue
        if key in ("workflow_data", "steps", "blocks"):
            payload[key] = json.dumps(value, ensure_ascii=False)
        else:
            payload[key] = str(value)
    await redis.hset(_meta_key(generation_id), mapping=payload)
    await _expire_keys(generation_id)


async def get_generation_meta(generation_id: str) -> dict[str, Any] | None:
    redis = get_redis()
    raw = await redis.hgetall(_meta_key(generation_id))
    if not raw:
        return None
    data = meta_to_dict(raw)
    data["generation_id"] = generation_id
    return data


async def update_generation_meta(generation_id: str, fields: dict[str, Any]) -> None:
    redis = get_redis()
    payload: dict[str, str] = {}
    for key, value in fields.items():
        if value is None:
            continue
        if key in ("workflow_data", "steps", "blocks"):
            payload[key] = json.dumps(value, ensure_ascii=False)
        else:
            payload[key] = str(value)
    if not payload:
        return
    await redis.hset(_meta_key(generation_id), mapping=payload)
    await redis.expire(_meta_key(generation_id), _ttl())


async def set_session_running(creator: str, session_id: str, generation_id: str) -> None:
    redis = get_redis()
    key = _session_running_key(creator, session_id)
    await redis.set(key, generation_id, ex=_ttl())


async def get_session_running(creator: str, session_id: str) -> str | None:
    redis = get_redis()
    value = await redis.get(_session_running_key(creator, session_id))
    return value or None


async def clear_session_running(creator: str, session_id: str) -> None:
    redis = get_redis()
    await redis.delete(_session_running_key(creator, session_id))


async def append_chunks_batch(
    generation_id: str,
    lines: list[str],
    *,
    content: str | None = None,
    reasoning_content: str | None = None,
    blocks: list[Any] | None = None,
    offset: int | None = None,
) -> None:
    if not lines:
        return
    redis = get_redis()
    chunks_key = _chunks_key(generation_id)
    await redis.rpush(chunks_key, *lines)
    meta_fields: dict[str, Any] = {}
    if offset is not None:
        meta_fields["offset"] = offset
    if content is not None:
        meta_fields["content"] = content
    if reasoning_content is not None:
        meta_fields["reasoning_content"] = reasoning_content
    if blocks is not None:
        meta_fields["blocks"] = blocks
    if meta_fields:
        await update_generation_meta(generation_id, meta_fields)
    await redis.rpush(_signal_key(generation_id), "1")
    await _expire_keys(generation_id)


async def get_chunks_range(generation_id: str, start: int, end: int) -> list[str]:
    redis = get_redis()
    if end < start:
        return []
    return await redis.lrange(_chunks_key(generation_id), start, end)


async def get_chunks_len(generation_id: str) -> int:
    redis = get_redis()
    return int(await redis.llen(_chunks_key(generation_id)))


async def wait_signal(generation_id: str, timeout_sec: float) -> bool:
    redis = get_redis()
    result = await redis.brpop(_signal_key(generation_id), timeout=timeout_sec)
    return result is not None


async def notify_generation_signal(generation_id: str) -> None:
    redis = get_redis()
    await redis.rpush(_signal_key(generation_id), "1")
    await redis.expire(_signal_key(generation_id), _ttl())


async def request_generation_cancel(generation_id: str) -> None:
    redis = get_redis()
    await redis.set(_cancel_key(generation_id), "1", ex=_ttl())
    await notify_generation_signal(generation_id)


async def is_generation_cancel_requested(generation_id: str) -> bool:
    redis = get_redis()
    value = await redis.get(_cancel_key(generation_id))
    return value is not None


async def clear_generation_cancel_requested(generation_id: str) -> None:
    redis = get_redis()
    await redis.delete(_cancel_key(generation_id))


async def delete_generation(generation_id: str) -> None:
    redis = get_redis()
    await redis.delete(
        _chunks_key(generation_id),
        _meta_key(generation_id),
        _signal_key(generation_id),
        _payload_key(generation_id),
        _checkpoint_key(generation_id),
        _cancel_key(generation_id),
    )


async def save_generation_checkpoint(generation_id: str, checkpoint: dict[str, Any]) -> None:
    redis = get_redis()
    await redis.set(
        _checkpoint_key(generation_id),
        json.dumps(checkpoint, ensure_ascii=False),
        ex=_ttl(),
    )


async def get_generation_checkpoint(generation_id: str) -> dict[str, Any] | None:
    redis = get_redis()
    raw = await redis.get(_checkpoint_key(generation_id))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def delete_generation_checkpoint(generation_id: str) -> None:
    redis = get_redis()
    await redis.delete(_checkpoint_key(generation_id))


async def save_generation_payload(generation_id: str, payload: dict[str, Any]) -> None:
    redis = get_redis()
    await redis.set(
        _payload_key(generation_id),
        json.dumps(payload, ensure_ascii=False),
        ex=_ttl(),
    )


async def get_generation_payload(generation_id: str) -> dict[str, Any] | None:
    redis = get_redis()
    raw = await redis.get(_payload_key(generation_id))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def delete_generation_payload(generation_id: str) -> None:
    redis = get_redis()
    await redis.delete(_payload_key(generation_id))
