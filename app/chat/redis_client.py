# -*- coding: utf-8 -*-
#
# Chat 模块 Redis 连接（异步）
#
from __future__ import annotations

from functools import lru_cache
from typing import Any

from redis.asyncio import Redis

from common import config_file

_DEFAULT_FLUSH_MS = 150
_DEFAULT_BATCH_BYTES = 32768
_DEFAULT_TTL = 86400
_DEFAULT_PREFIX = "chat:gen"


def _chat_redis_settings() -> dict[str, Any]:
    conf = config_file.read_conf(config_file.config_dir) or {}
    chat_redis = conf.get("chat_redis") or {}
    url = (chat_redis.get("url") or "").strip()
    if not url:
        celery = conf.get("celery") or {}
        url = (celery.get("result_backend") or "").strip()
    if not url:
        url = "redis://127.0.0.1:6379/1"
    return {
        "url": url,
        "key_prefix": (chat_redis.get("key_prefix") or _DEFAULT_PREFIX).strip(),
        "ttl_seconds": int(chat_redis.get("ttl_seconds") or _DEFAULT_TTL),
        "flush_interval_ms": int(chat_redis.get("flush_interval_ms") or _DEFAULT_FLUSH_MS),
        "flush_batch_bytes": int(chat_redis.get("flush_batch_bytes") or _DEFAULT_BATCH_BYTES),
        "signal_timeout_sec": float(chat_redis.get("signal_timeout_sec") or 0.3),
        "action_confirm_timeout_sec": int(chat_redis.get("action_confirm_timeout_sec") or 300),
        "action_confirm_stream_grace_sec": float(
            chat_redis.get("action_confirm_stream_grace_sec") or 30
        ),
    }


@lru_cache(maxsize=1)
def get_chat_redis_settings() -> dict[str, Any]:
    return _chat_redis_settings()


_redis_client: Redis | None = None


def get_redis() -> Redis:
    global _redis_client
    if _redis_client is None:
        settings = get_chat_redis_settings()
        _redis_client = Redis.from_url(
            settings["url"],
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
    return _redis_client


async def close_redis() -> None:
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None
