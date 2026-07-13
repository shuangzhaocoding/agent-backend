# -*- coding: utf-8 -*-
#
# Jira 建单确认与执行
#
from typing import Any, Optional

from agent.router import generate_jira_summary_by_deepseek

from chat.jira.service import create_jira_issue

JIRA_CONFIRM_API_PATH = "/api/chat/jira/confirm"


def validate_jira_draft(draft: dict[str, Any]) -> str | None:
    """校验 draft 必填字段，返回错误信息；通过则返回 None。"""
    if not (draft.get("product_type") or "").strip():
        return "缺少 product_type（产品型号）"
    if not (draft.get("description") or "").strip():
        return "缺少 description（问题描述）"
    if not (draft.get("sn") or "").strip() and not (draft.get("did") or "").strip():
        return "sn 与 did 至少填写一项"
    return None


def build_confirm_api_hint() -> dict[str, str]:
    return {
        "method": "POST",
        "path": JIRA_CONFIRM_API_PATH,
        "description": "将 workflow_data.draft 原样放入请求体 draft 字段后调用",
    }


def format_jira_preview(draft: dict[str, Any], robot_info: dict[str, Any] | None = None) -> str:
    robot_info = robot_info or {}
    attachment_count = len(draft.get("resources") or [])
    lines = [
        "已为您准备好 Jira 工单草稿，尚未创建，请确认以下信息：",
        "",
        f"- 产品型号：{draft.get('product_type') or '-'}",
        f"- 标题摘要：{draft.get('summary') or '-'}",
        f"- 问题描述：{draft.get('description') or '-'}",
        f"- SN：{draft.get('sn') or '-'}",
        f"- DID：{draft.get('did') or '-'}",
        f"- 固件版本：{draft.get('firmware_version') or robot_info.get('firmware_version') or '-'}",
        f"- 问题发生时间：{draft.get('issue_time') or '-'}",
        f"- APP 版本：{draft.get('app_version') or '-'}",
        f"- APPID：{draft.get('app_id') or '-'}",
        f"- 附件数量：{attachment_count}",
        "",
        "请确认以上信息无误后，点击「确认创建」提交工单；如需修改，请补充说明后重新提单。",
    ]
    return "\n".join(lines)


async def build_jira_draft(
    *,
    product_type: str,
    description: str,
    user_email: str,
    sn: str = "",
    did: str = "",
    firmware_version: str = "",
    region: str = "cn",
    bill_code: str | None = None,
    issue_time: str | None = None,
    app_version: str = "",
    app_id: str = "",
    resources: list[str] | None = None,
    robot_info: dict[str, Any] | None = None,
    issues_count: int | None = None,
    issue_index: int | None = None,
) -> dict[str, Any]:
    try:
        summary = await generate_jira_summary_by_deepseek(description)
    except Exception:
        summary = (description or "")[:80]

    return {
        "product_type": product_type,
        "description": description,
        "user_email": user_email,
        "sn": sn,
        "did": did,
        "firmware_version": firmware_version,
        "region": region,
        "bill_code": bill_code,
        "issue_time": issue_time,
        "app_version": app_version,
        "app_id": app_id,
        "resources": resources or [],
        "summary": summary,
        "robot_info": robot_info or {},
        "issues_count": issues_count,
        "issue_index": issue_index,
    }


async def execute_jira_create_from_draft(
    draft: dict[str, Any],
    *,
    user_email: Optional[str] = None,
) -> dict[str, Any]:
    email = (user_email or draft.get("user_email") or "").strip()
    if not email:
        return {"success": False, "status": "failed", "error": "missing_user_email"}

    validation_error = validate_jira_draft(draft)
    if validation_error:
        return {"success": False, "status": "invalid_draft", "error": validation_error}

    jira_result = await create_jira_issue(
        product_type=draft.get("product_type") or "",
        description=draft.get("description") or "",
        user_email=email,
        sn=draft.get("sn") or None,
        did=draft.get("did") or None,
        firmware_version=draft.get("firmware_version") or None,
        region=draft.get("region") or "cn",
        bill_code=draft.get("bill_code"),
        issue_time=draft.get("issue_time"),
        app_version=draft.get("app_version") or None,
        app_id=draft.get("app_id") or None,
        resources=draft.get("resources") or [],
    )

    if not jira_result.get("success"):
        return {
            "success": False,
            "status": "failed",
            **jira_result,
        }

    robot_info = draft.get("robot_info") if isinstance(draft.get("robot_info"), dict) else {}
    workflow_data: dict[str, Any] = {
        "success": True,
        "status": "created",
        **jira_result,
        "robot_info": {
            "product_name": robot_info.get("product_name"),
            "firmware_version": draft.get("firmware_version"),
        },
        "attachment_count": len(draft.get("resources") or []),
    }
    issues_count = draft.get("issues_count")
    issue_index = draft.get("issue_index")
    if isinstance(issues_count, int) and issues_count > 1:
        workflow_data["issues_count"] = issues_count
        workflow_data["issue_index"] = issue_index
        workflow_data["note"] = (
            f"输入中共识别 {issues_count} 条问题，"
            f"已创建第 {int(issue_index or 0) + 1} 条对应 Jira 单。"
            "其余问题请再次提单或设置 context.issue_index。"
        )
    return workflow_data


async def confirm_jira_issue(
    draft: dict[str, Any],
    *,
    user_email: str,
) -> dict[str, Any]:
    """独立确认接口入口：校验 draft 并创建 Jira 单。"""
    return await execute_jira_create_from_draft(draft, user_email=user_email)
