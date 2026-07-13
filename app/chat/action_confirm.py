# -*- coding: utf-8 -*-
#
# 统一人机确认：需确认的工具 / workflow 动作
#
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from common.logger import logger

from chat.jira.confirm import execute_jira_create_from_draft, validate_jira_draft
from chat.redis_client import get_chat_redis_settings
from chat.tool_registry import ToolRegistry
from chat.tools import execute_tool

ACTIONS_RESPOND_API_PATH = "/api/chat/actions/respond"

ACTION_TYPE_CREATE_JIRA = "create_jira"

CONFIRM_REQUIRED_TOOLS = ToolRegistry.confirm_required_names()

ACTION_TITLES: dict[str, str] = {
    ACTION_TYPE_CREATE_JIRA: "创建 Jira 工单",
    **ToolRegistry.action_titles(),
}


def build_actions_respond_hint() -> dict[str, str]:
    return {
        "method": "POST",
        "path": ACTIONS_RESPOND_API_PATH,
        "description": "用户确认或取消后调用；approved=true 时可将修改后的 draft 放入 draft 字段",
    }


def get_action_confirm_timeout_sec() -> int:
    settings = get_chat_redis_settings()
    timeout_sec = int(settings.get("action_confirm_timeout_sec") or 300)
    return max(timeout_sec, 1)


def build_action_confirm_timing() -> dict[str, int]:
    timeout_sec = get_action_confirm_timeout_sec()
    paused_at = int(time.time())
    return {
        "confirm_timeout_sec": timeout_sec,
        "paused_at": paused_at,
        "expires_at": paused_at + timeout_sec,
    }


def build_action_required_payload(
    *,
    action_type: str,
    draft: dict[str, Any],
    preview_text: str,
    kind: str,
    mode: str | None = None,
    intent: str | None = None,
    steps: list[Any] | None = None,
    action_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action_id = action_id or str(uuid.uuid4())
    payload: dict[str, Any] = {
        "type": "action_required",
        "action_id": action_id,
        "action_type": action_type,
        "title": ACTION_TITLES.get(action_type, action_type),
        "draft": draft,
        "preview_text": preview_text,
        "kind": kind,
        "respond_api": build_actions_respond_hint(),
    }
    if mode:
        payload["mode"] = mode
    if intent:
        payload["intent"] = intent
    if steps is not None:
        payload["steps"] = steps
    if extra:
        payload.update(extra)
    payload.update(build_action_confirm_timing())
    return payload


def build_checkpoint(
    *,
    action_id: str,
    action_type: str,
    draft: dict[str, Any],
    kind: str,
    preview_text: str = "",
    mode: str | None = None,
    intent: str | None = None,
    steps: list[Any] | None = None,
    react_messages: list[Any] | None = None,
    pending_tool_call: dict[str, Any] | None = None,
    remaining_tool_calls: list[Any] | None = None,
    step_idx: int | None = None,
    user_email: str | None = None,
    confirm_timeout_sec: int | None = None,
    paused_at: int | None = None,
    expires_at: int | None = None,
    selected_skill_ids: list[str] | None = None,
    direct_reply: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "action_id": action_id,
        "action_type": action_type,
        "draft": draft,
        "kind": kind,
        "preview_text": preview_text,
        "mode": mode,
        "intent": intent,
        "steps": steps or [],
        "react_messages": react_messages,
        "pending_tool_call": pending_tool_call,
        "remaining_tool_calls": remaining_tool_calls,
        "step_idx": step_idx,
        "user_email": user_email,
        "confirm_timeout_sec": confirm_timeout_sec,
        "paused_at": paused_at,
        "expires_at": expires_at,
    }
    if selected_skill_ids is not None:
        data["selected_skill_ids"] = selected_skill_ids
    if direct_reply is not None:
        data["direct_reply"] = direct_reply
    if extra:
        data["extra"] = extra
    return data


async def execute_confirmed_action(
    action_type: str,
    draft: dict[str, Any],
    *,
    user_email: str | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """用户确认后执行动作，返回 observation / workflow 结果。"""
    context = context or {}

    if action_type == ACTION_TYPE_CREATE_JIRA:
        if user_email:
            draft = dict(draft)
            draft.setdefault("user_email", user_email)
        err = validate_jira_draft(draft)
        if err:
            return {"success": False, "error": err}
        return await execute_jira_create_from_draft(draft, user_email=user_email or "")

    if action_type in CONFIRM_REQUIRED_TOOLS:
        try:
            observation = await execute_tool(action_type, draft, context=context)
            return {"success": True, "observation": observation}
        except Exception as exc:
            logger.error(f"确认后工具执行失败 tool={action_type}", exc_info=True)
            return {"success": False, "error": str(exc), "observation": {"error": str(exc)}}

    return {"success": False, "error": f"未知动作类型: {action_type}"}


def format_action_result_text(action_type: str, result: dict[str, Any]) -> str:
    if not result.get("success"):
        return f"执行失败：{result.get('error') or '未知错误'}"

    if action_type == ACTION_TYPE_CREATE_JIRA:
        key = result.get("key") or result.get("id") or ""
        url = result.get("jira_url") or ""
        lines = ["Jira 工单已创建成功。"]
        if key:
            lines.append(f"- 工单号：{key}")
        if url:
            lines.append(f"- 链接：{url}")
        note = result.get("note")
        if note:
            lines.append(str(note))
        return "\n".join(lines)

    observation = result.get("observation") or {}
    if not isinstance(observation, dict):
        return str(observation)

    if action_type == "add_users_to_app":
        return _format_add_users_to_app_result(observation)
    if action_type == "switch_bag_upload":
        return _format_switch_bag_upload_result(observation)
    if action_type == "push_firmware":
        return _format_push_firmware_result(observation)

    if observation.get("error"):
        return f"执行完成，但返回错误：{observation.get('error')}"
    message = observation.get("message")
    if message:
        return str(message)
    return json.dumps(observation, ensure_ascii=False, indent=2)


def _format_appid_item_line(item: dict[str, Any]) -> str:
    appid = item.get("appid")
    name = str(item.get("name") or "").strip()
    if appid is None:
        return str(item)
    if name and name != str(appid):
        return f"- {appid}（{name}）"
    return f"- {appid}"


def _format_add_users_to_app_result(observation: dict[str, Any]) -> str:
    success_items = observation.get("success_items") or []
    failed_items = observation.get("failed_items") or []
    error_text = observation.get("error")

    if error_text and not success_items:
        return f"APPID 加白失败：{error_text}"

    lines: list[str] = []
    if error_text and success_items:
        lines.append("部分 APPID 加白成功。")
    else:
        lines.append(observation.get("message") or "APPID 加白成功。")

    if success_items:
        lines.append(f"已成功 {len(success_items)} 个：")
        for item in success_items:
            if isinstance(item, dict):
                lines.append(_format_appid_item_line(item))

    if failed_items:
        lines.append(f"失败 {len(failed_items)} 个：")
        for item in failed_items:
            if isinstance(item, dict):
                lines.append(_format_appid_item_line(item))

    if error_text and success_items:
        lines.append(f"说明：{error_text}")

    return "\n".join(lines)


def _format_switch_bag_upload_result(observation: dict[str, Any]) -> str:
    error_text = observation.get("error")
    if error_text:
        device = observation.get("did_or_sn") or ""
        prefix = f"设备 {device}：" if device else ""
        return f"BAG 日志上传切换失败：{prefix}{error_text}"

    device = observation.get("did_or_sn") or ""
    data = observation.get("data") if isinstance(observation.get("data"), dict) else {}
    status_text = data.get("upload_status_text")
    if not status_text and observation.get("upload_status") is not None:
        status_text = "已开启立即上传" if observation.get("upload_status") else "未开启立即上传"

    lines = ["BAG 日志上传设置已更新。"]
    if device:
        lines.append(f"- 设备：{device}")
    if status_text:
        lines.append(f"- 状态：{status_text}")

    message = observation.get("message")
    if message and str(message).strip().lower() not in {"ok", "success", "成功"}:
        lines.append(str(message))

    return "\n".join(lines)


def _format_push_firmware_result(observation: dict[str, Any]) -> str:
    error_text = observation.get("error")
    device = observation.get("did_or_sn") or ""
    firmware_id = observation.get("firmware_id") or ""
    if error_text:
        prefix = f"设备 {device}：" if device else ""
        return f"固件推送失败：{prefix}{error_text}"

    lines = [observation.get("message") or "固件推送成功。"]
    if device:
        lines.append(f"- 设备：{device}")
    if firmware_id:
        lines.append(f"- 固件 ID：{firmware_id}")
    return "\n".join(lines)


def format_cancel_text(action_type: str) -> str:
    title = ACTION_TITLES.get(action_type, action_type)
    return f"已取消：{title}，未执行任何变更。"


def format_timeout_text(action_type: str) -> str:
    title = ACTION_TITLES.get(action_type, action_type)
    timeout_sec = get_action_confirm_timeout_sec()
    return f"确认已超时（{timeout_sec} 秒）：{title}，未执行任何变更。"
