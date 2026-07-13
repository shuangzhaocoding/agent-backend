# -*- coding: utf-8 -*-
#
# Assistant 消息有序 blocks（reasoning → text → action_card → …）
#
from __future__ import annotations

import copy
from typing import Any

from chat.action_confirm import build_actions_respond_hint


def flatten_blocks(blocks: list[Any]) -> str:
    """将 blocks 拼接为 content 兼容字段。"""
    parts: list[str] = []
    for item in blocks:
        if not isinstance(item, dict):
            continue
        block_type = item.get("type")
        if block_type == "text":
            text = str(item.get("content") or "")
            if text:
                parts.append(text)
        elif block_type == "action_card":
            result_text = str(item.get("result_text") or "")
            if result_text:
                parts.append(result_text)
    return "".join(parts)


def flatten_reasoning_blocks(blocks: list[Any]) -> str:
    """将 reasoning blocks 拼接为 reasoning_content 兼容字段。"""
    parts: list[str] = []
    for item in blocks:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "reasoning":
            continue
        text = str(item.get("content") or "")
        if text:
            parts.append(text)
    return "".join(parts)


def sync_derived_from_blocks(blocks: list[Any]) -> tuple[str, str]:
    """从 blocks 派生 content 与 reasoning_content。"""
    return flatten_blocks(blocks), flatten_reasoning_blocks(blocks)


def append_text_block(blocks: list[dict[str, Any]], content: str) -> None:
    text = (content or "").strip()
    if not text:
        return
    blocks.append({"type": "text", "content": text})


def extend_last_text_block(blocks: list[dict[str, Any]], delta: str) -> None:
    if not delta:
        return
    if blocks and blocks[-1].get("type") == "text":
        blocks[-1]["content"] = str(blocks[-1].get("content") or "") + delta
        return
    blocks.append({"type": "text", "content": delta})


def append_reasoning_block(blocks: list[dict[str, Any]], content: str) -> None:
    text = (content or "").strip()
    if not text:
        return
    blocks.append({"type": "reasoning", "content": text})


def extend_last_reasoning_block(blocks: list[dict[str, Any]], delta: str) -> None:
    if not delta:
        return
    if blocks and blocks[-1].get("type") == "reasoning":
        blocks[-1]["content"] = str(blocks[-1].get("content") or "") + delta
        return
    blocks.append({"type": "reasoning", "content": delta})


def seal_last_reasoning_block(
    blocks: list[dict[str, Any]],
    thinking_ms: int | None,
) -> bool:
    """将本轮思考耗时写入最近一个 reasoning block（支持多段思考）。"""
    if thinking_ms is None:
        return False
    ms = max(0, int(thinking_ms))
    for item in reversed(blocks):
        if not isinstance(item, dict):
            continue
        if item.get("type") != "reasoning":
            continue
        item["thinking_ms"] = ms
        return True
    return False


def build_action_card_block(
    action_data: dict[str, Any],
    *,
    status: str = "pending",
    result_text: str = "",
) -> dict[str, Any]:
    block: dict[str, Any] = {
        "type": "action_card",
        "action_id": str(action_data.get("action_id") or ""),
        "action_type": str(action_data.get("action_type") or ""),
        "title": action_data.get("title") or action_data.get("action_type") or "",
        "draft": copy.deepcopy(action_data.get("draft") or {}),
        "kind": action_data.get("kind") or "",
        "status": status,
        "confirm_timeout_sec": action_data.get("confirm_timeout_sec"),
        "paused_at": action_data.get("paused_at"),
        "expires_at": action_data.get("expires_at"),
        "respond_api": action_data.get("respond_api") or build_actions_respond_hint(),
    }
    if result_text:
        block["result_text"] = result_text
    return block


def append_action_card_block(
    blocks: list[dict[str, Any]],
    action_data: dict[str, Any],
    *,
    status: str = "pending",
    result_text: str = "",
) -> dict[str, Any]:
    block = build_action_card_block(action_data, status=status, result_text=result_text)
    blocks.append(block)
    return block


def update_action_card_block(
    blocks: list[dict[str, Any]],
    action_id: str,
    *,
    status: str | None = None,
    result_text: str | None = None,
    draft: dict[str, Any] | None = None,
) -> bool:
    target_id = str(action_id or "")
    if not target_id:
        return False
    for block in blocks:
        if block.get("type") != "action_card":
            continue
        if str(block.get("action_id") or "") != target_id:
            continue
        if status is not None:
            block["status"] = status
        if result_text is not None:
            block["result_text"] = result_text
        if draft is not None:
            block["draft"] = copy.deepcopy(draft)
        return True
    return False


def apply_block_ops(blocks: list[dict[str, Any]], ops: list[Any]) -> None:
    """按顺序应用 block 操作。"""
    for op in ops or []:
        if not isinstance(op, dict):
            continue
        name = op.get("op")
        if name == "append_text":
            append_text_block(blocks, str(op.get("content") or ""))
        elif name == "append_reasoning":
            append_reasoning_block(blocks, str(op.get("content") or ""))
        elif name == "seal_reasoning":
            raw_ms = op.get("thinking_ms")
            try:
                ms = int(raw_ms) if raw_ms is not None else None
            except (TypeError, ValueError):
                ms = None
            seal_last_reasoning_block(blocks, ms)
        elif name == "append_action_card":
            append_action_card_block(
                blocks,
                op.get("action_data") or {},
                status=str(op.get("status") or "pending"),
                result_text=str(op.get("result_text") or ""),
            )
        elif name == "update_card":
            update_action_card_block(
                blocks,
                str(op.get("action_id") or ""),
                status=op.get("status"),
                result_text=op.get("result_text"),
                draft=op.get("draft") if isinstance(op.get("draft"), dict) else None,
            )


def load_blocks_from_message_metadata(metadata: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(metadata, dict):
        return []
    raw = metadata.get("blocks")
    if not isinstance(raw, list):
        return []
    return copy.deepcopy([item for item in raw if isinstance(item, dict)])


def merge_assistant_message_from_session(
    incoming: dict[str, Any],
    existing: dict[str, Any],
) -> None:
    """合并 DB 中已有 assistant 消息，避免新一轮写入覆盖 blocks 等元数据。"""
    existing_meta = existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}
    incoming_meta = dict(incoming.get("metadata") or {})

    existing_blocks = existing_meta.get("blocks")
    incoming_blocks = incoming_meta.get("blocks")
    if isinstance(existing_blocks, list) and existing_blocks:
        if not isinstance(incoming_blocks, list) or not incoming_blocks:
            incoming_meta["blocks"] = copy.deepcopy(existing_blocks)

    for key in ("mode", "intent", "status"):
        if not incoming_meta.get(key) and existing_meta.get(key):
            incoming_meta[key] = existing_meta[key]

    existing_wf = existing_meta.get("workflow_data")
    incoming_wf = incoming_meta.get("workflow_data")
    if isinstance(existing_wf, dict) and existing_wf:
        if not isinstance(incoming_wf, dict) or not incoming_wf:
            incoming_meta["workflow_data"] = copy.deepcopy(existing_wf)

    if not incoming_meta.get("id") and existing_meta.get("id"):
        incoming_meta["id"] = existing_meta["id"]

    incoming["metadata"] = incoming_meta

    if not str(incoming.get("content") or "").strip() and str(existing.get("content") or "").strip():
        incoming["content"] = existing.get("content")
    if not incoming.get("reasoning_content") and existing.get("reasoning_content"):
        incoming["reasoning_content"] = existing.get("reasoning_content")
    elif not incoming.get("reasoning_content"):
        merged_blocks = incoming_meta.get("blocks")
        if isinstance(merged_blocks, list) and merged_blocks:
            reasoning = flatten_reasoning_blocks(merged_blocks)
            if reasoning:
                incoming["reasoning_content"] = reasoning


def preserve_session_assistant_metadata(
    messages: list[dict[str, Any]],
    existing_messages: list[Any],
) -> None:
    """将 session 中已有 assistant 元数据合并进即将写入的消息列表。"""
    existing_assistants = [
        item
        for item in existing_messages
        if isinstance(item, dict) and item.get("role") == "assistant"
    ]
    if not existing_assistants:
        return

    existing_by_id: dict[str, dict[str, Any]] = {}
    for item in existing_assistants:
        mid = str((item.get("metadata") or {}).get("id") or "")
        if mid:
            existing_by_id[mid] = item

    assistant_idx = 0
    for item in messages:
        if item.get("role") != "assistant":
            continue

        existing: dict[str, Any] | None = None
        mid = str((item.get("metadata") or {}).get("id") or "")
        if mid and mid in existing_by_id:
            existing = existing_by_id[mid]
        elif assistant_idx < len(existing_assistants):
            existing = existing_assistants[assistant_idx]

        if existing:
            merge_assistant_message_from_session(item, existing)
        assistant_idx += 1
