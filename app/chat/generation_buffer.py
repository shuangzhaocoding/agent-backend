# -*- coding: utf-8 -*-
#
# 进程内生成缓冲区 + Redis 批量刷写
#
from __future__ import annotations

import asyncio
from typing import Any

from common.logger import logger

from chat.message_blocks import (
    extend_last_reasoning_block,
    extend_last_text_block,
    sync_derived_from_blocks,
)
from chat.redis_client import get_chat_redis_settings
from chat.redis_generation import append_chunks_batch

_buffers: dict[str, GenerationBuffer] = {}
_flusher_task: asyncio.Task | None = None


class GenerationBuffer:
    """单 generation 内存缓冲：SSE 热路径 + 订阅者 fan-out。"""

    def __init__(
        self,
        generation_id: str,
        *,
        session_id: str,
        message_id: str,
        chunk_id: str,
        creator: str,
    ) -> None:
        self.generation_id = generation_id
        self.session_id = session_id
        self.message_id = message_id
        self.chunk_id = chunk_id
        self.creator = creator
        self.chunks: list[str] = []
        self.blocks: list[dict[str, Any]] = []
        self.content = ""
        self.reasoning_content = ""
        self.status = "running"
        self.mode: str | None = None
        self.intent: str | None = None
        self.workflow_data: dict[str, Any] = {}
        self.steps: list[Any] = []
        self.error: str | None = None
        self.completed_at: str | None = None
        self.thinking_ms: int | None = None
        self.duration_ms: int | None = None
        self.pending_redis: list[str] = []
        self.subscribers: list[tuple[asyncio.Queue, int]] = []
        self.lock = asyncio.Lock()
        self._flush_lock = asyncio.Lock()

    async def append(
        self,
        sse_line: str,
        *,
        content_delta: str = "",
        reasoning_delta: str = "",
        update_blocks: bool = True,
    ) -> int:
        async with self.lock:
            idx = len(self.chunks)
            self.chunks.append(sse_line)
            if content_delta and update_blocks:
                extend_last_text_block(self.blocks, content_delta)
            if reasoning_delta and update_blocks:
                extend_last_reasoning_block(self.blocks, reasoning_delta)
            if update_blocks and (content_delta or reasoning_delta):
                self.content, self.reasoning_content = sync_derived_from_blocks(self.blocks)
            self.pending_redis.append(sse_line)
            for queue, min_idx in self.subscribers:
                if idx >= min_idx:
                    try:
                        queue.put_nowait((idx, sse_line))
                    except asyncio.QueueFull:
                        logger.warning(
                            f"generation 订阅队列已满 generation_id={self.generation_id} idx={idx}"
                        )
            return idx

    def subscribe(self, min_offset: int) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self.subscribers.append((queue, min_offset))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers = [(q, m) for q, m in self.subscribers if q is not queue]

    async def complete(
        self,
        status: str,
        *,
        mode: str | None = None,
        intent: str | None = None,
        workflow_data: dict[str, Any] | None = None,
        steps: list[Any] | None = None,
        content: str | None = None,
        reasoning_content: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
        error: str | None = None,
    ) -> None:
        async with self.lock:
            self.status = status
            if mode is not None:
                self.mode = mode
            if intent is not None:
                self.intent = intent
            if workflow_data is not None:
                self.workflow_data = workflow_data
            if steps is not None:
                self.steps = steps
            if blocks is not None:
                self.blocks = blocks
                self.content, self.reasoning_content = sync_derived_from_blocks(blocks)
            elif content is not None:
                self.content = content
            if reasoning_content is not None and blocks is None:
                self.reasoning_content = reasoning_content
            if error is not None:
                self.error = error
            for queue, _ in self.subscribers:
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    async def flush_to_redis(self, force: bool = False) -> None:
        async with self._flush_lock:
            async with self.lock:
                if not self.pending_redis and not force:
                    return
                pending = list(self.pending_redis)
                self.pending_redis.clear()
                offset = len(self.chunks)
                content = self.content
                reasoning = self.reasoning_content
                blocks = list(self.blocks)
            if not pending and not force:
                return
            try:
                await append_chunks_batch(
                    self.generation_id,
                    pending,
                    content=content,
                    reasoning_content=reasoning,
                    blocks=blocks,
                    offset=offset,
                )
            except Exception as exc:
                logger.error(
                    f"刷写 Redis 失败 generation_id={self.generation_id}: {exc}",
                    exc_info=True,
                )
                async with self.lock:
                    self.pending_redis = pending + self.pending_redis

    async def apply_block_ops(self, ops: list[Any]) -> None:
        from chat.message_blocks import apply_block_ops as _apply

        async with self.lock:
            _apply(self.blocks, ops)
            self.content, self.reasoning_content = sync_derived_from_blocks(self.blocks)


def get_buffer(generation_id: str) -> GenerationBuffer | None:
    return _buffers.get(generation_id)


def register_buffer(buffer: GenerationBuffer) -> GenerationBuffer:
    _buffers[buffer.generation_id] = buffer
    ensure_flusher_running()
    return buffer


def remove_buffer(generation_id: str) -> None:
    _buffers.pop(generation_id, None)


def ensure_flusher_running() -> None:
    global _flusher_task
    if _flusher_task is not None and not _flusher_task.done():
        return
    _flusher_task = asyncio.create_task(_flusher_loop())


async def _flusher_loop() -> None:
    settings = get_chat_redis_settings()
    interval = max(settings["flush_interval_ms"] / 1000.0, 0.05)
    batch_bytes = int(settings["flush_batch_bytes"])
    while True:
        await asyncio.sleep(interval)
        for generation_id in list(_buffers.keys()):
            buffer = _buffers.get(generation_id)
            if not buffer:
                continue
            pending_bytes = sum(len(line) for line in buffer.pending_redis)
            if pending_bytes >= batch_bytes or buffer.pending_redis:
                await buffer.flush_to_redis()


async def flush_buffer(generation_id: str, force: bool = True) -> None:
    buffer = _buffers.get(generation_id)
    if buffer:
        await buffer.flush_to_redis(force=force)
