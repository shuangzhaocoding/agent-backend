# -*- coding: utf-8 -*-
#
# 创建 Jira 单 workflow
#
import re
import uuid
from typing import Any, AsyncIterator

from common.logger import logger
from device_module.router import get_robot_info_api, get_sn_info_api
from j1_module.router import get_sn_info_api_j1
from j2_module.router import get_sn_info_api_j2
from schema import CodeEnum

from chat.action_confirm import ACTION_TYPE_CREATE_JIRA, build_action_required_payload
from chat.common.workflow import emit_workflow_finish, WorkflowRunResult
from chat.intent import IntentResult
from chat.jira.confirm import (
    build_jira_draft,
    format_jira_preview,
)
from chat.jira.extract import extract_jira_fields_by_llm, merge_jira_issue_fields
from chat.react import ReactEvent, ReactStepRecord
from chat.schemas import AgentChatRequest


def _get_user_email(context: dict[str, Any] | None) -> str:
    context = context or {}
    return (
        context.get("user_email")
        or context.get("email")
        or context.get("reporter")
        or ""
    ).strip()


DID_PATTERN = re.compile(r"^[a-f0-9]{32}$", re.IGNORECASE)


def _is_device_did(value: str) -> bool:
    return bool(DID_PATTERN.match((value or "").strip()))


def _robot_sn_from_sn_info(data: dict[str, Any], source: str) -> str:
    if source in ("j1", "j2"):
        return (data.get("robot_sn") or "").strip()

    sn_list = data.get("sn_info")
    if not isinstance(sn_list, list):
        return ""

    preferred_labels = ("机器人SN", "洗地机整机SN")
    for label in preferred_labels:
        for item in sn_list:
            if isinstance(item, dict) and item.get("label") == label:
                sn = (item.get("sn") or "").strip()
                if sn:
                    return sn
    return ""


async def _query_sn_info(sn_number: str) -> tuple[dict[str, Any], str, str | None]:
    """查询三码，依次尝试 MES(J3+)、J2、J1。返回 (data, source, error)。"""
    last_error = "查无三码/关联 SN 信息"
    for api_func, source in (
        (get_sn_info_api, "mes"),
        (get_sn_info_api_j2, "j2"),
        (get_sn_info_api_j1, "j1"),
    ):
        try:
            response = await api_func(sn_number=sn_number)
        except Exception as exc:
            logger.warning(f"三码查询失败 source={source} sn={sn_number}: {exc}")
            last_error = str(exc)
            continue

        if response and response.code == CodeEnum.SUCCESS:
            return response.data or {}, source, None

        last_error = getattr(response, "message", None) or last_error

    return {}, "", last_error


async def run_create_jira_workflow(
    payload: AgentChatRequest,
    intent_result: IntentResult,
) -> AsyncIterator[ReactEvent]:
    context = payload.context or {}
    user_text = payload.get_current_user_content()
    slots = intent_result.slots or {}
    steps: list[ReactStepRecord] = []
    step_idx = 1

    yield ReactEvent(
        type="tool_start",
        step=step_idx,
        data={"tool_name": "workflow_create_jira", "action_input": {"phase": "llm_extract"}},
    )

    llm_result = await extract_jira_fields_by_llm(user_text)
    issue_index = int(context.get("issue_index") or 0)
    merged = merge_jira_issue_fields(
        llm_result,
        context=context,
        slots=slots,
        issue_index=issue_index,
    )
    issue = merged["issue"]
    sn = issue.get("sn") or ""
    did = issue.get("did") or ""
    description = issue.get("description") or ""
    issue_time = issue.get("issue_time") or ""
    firmware_version = issue.get("firmware_version") or ""
    app_version = issue.get("app_version") or ""
    app_id = issue.get("app_id") or ""
    did_or_sn = sn or did

    extract_obs = {
        "sn": sn,
        "did": did,
        "description": description,
        "issue_time": issue_time,
        "firmware_version": firmware_version,
        "app_version": app_version,
        "app_id": app_id,
        "issues_count": merged.get("issues_count"),
        "issue_index": merged.get("issue_index"),
        "extract_error": merged.get("extract_error"),
        "llm_issues": llm_result.get("issues"),
    }
    steps.append(
        ReactStepRecord(
            step=step_idx,
            thought="",
            action="llm_extract_jira_fields",
            action_input={"user_text": user_text},
            observation=extract_obs,
        )
    )
    yield ReactEvent(
        type="tool_done",
        step=step_idx,
        data={"tool_name": "llm_extract_jira_fields", "result": extract_obs},
    )

    if merged.get("issues_count", 0) > 1 and issue_index == 0:
        logger.info(
            f"用户输入含 {merged.get('issues_count')} 条问题记录，当前处理第 1 条（issue_index=0）"
        )

    if not did_or_sn:
        result = WorkflowRunResult(
            mode="workflow_create_jira",
            intent="create_jira",
            steps=steps,
            final_answer=(
                "创建 Jira 单需要机器人 SN 或设备 DID（deviceid）。"
                "请按格式补充【SN】或【deviceid】，并包含【问题描述】。"
            ),
            workflow_data={"success": False, "error": "missing_sn_or_did", **extract_obs},
        )
        async for event in emit_workflow_finish(payload, result):
            yield event
        return

    if not description:
        result = WorkflowRunResult(
            mode="workflow_create_jira",
            intent="create_jira",
            steps=steps,
            final_answer="请补充【问题描述】，以便创建 Jira 单。",
            workflow_data={"success": False, "error": "missing_description", **extract_obs},
        )
        async for event in emit_workflow_finish(payload, result):
            yield event
        return

    robot_info_key = did_or_sn
    sn_info_obs: dict[str, Any] | None = None

    if not _is_device_did(did_or_sn):
        step_idx += 1
        yield ReactEvent(
            type="tool_start",
            step=step_idx,
            data={"tool_name": "get_sn_info", "action_input": {"sn_number": did_or_sn}},
        )

        sn_info_data, sn_info_source, sn_info_error = await _query_sn_info(did_or_sn)
        if sn_info_error:
            sn_info_obs = {"error": sn_info_error}
        else:
            robot_sn = _robot_sn_from_sn_info(sn_info_data, sn_info_source)
            sn_info_obs = {
                "source": sn_info_source,
                "data": sn_info_data,
                "robot_sn": robot_sn,
            }
            if robot_sn:
                robot_info_key = robot_sn
                sn = sn or robot_sn
            else:
                sn_info_obs["error"] = "三码结果中未找到机器人 SN"

        steps.append(
            ReactStepRecord(
                step=step_idx,
                thought="",
                action="get_sn_info",
                action_input={"sn_number": did_or_sn},
                observation=sn_info_obs,
            )
        )
        yield ReactEvent(
            type="tool_done",
            step=step_idx,
            data={"tool_name": "get_sn_info", "result": sn_info_obs},
        )

        if sn_info_obs.get("error"):
            result = WorkflowRunResult(
                mode="workflow_create_jira",
                intent="create_jira",
                steps=steps,
                final_answer=f"无法查询三码信息：{sn_info_obs['error']}。请确认 SN 是否正确。",
                workflow_data={"success": False, "error": sn_info_obs["error"]},
            )
            async for event in emit_workflow_finish(payload, result):
                yield event
            return

    step_idx += 1
    yield ReactEvent(
        type="tool_start",
        step=step_idx,
        data={"tool_name": "get_robot_info", "action_input": {"did_or_sn": robot_info_key}},
    )

    try:
        robot_response = await get_robot_info_api(did_or_sn=robot_info_key)
    except Exception as exc:
        logger.error("查询设备信息失败", exc_info=True)
        robot_response = None
        robot_obs = {"error": str(exc)}
    else:
        if robot_response and robot_response.code == CodeEnum.SUCCESS:
            robot_obs = robot_response.data or {}
        else:
            robot_obs = {
                "error": getattr(robot_response, "message", None) or "查无设备信息",
            }

    steps.append(
        ReactStepRecord(
            step=step_idx,
            thought="",
            action="get_robot_info",
            action_input={"did_or_sn": robot_info_key},
            observation=robot_obs,
        )
    )
    yield ReactEvent(
        type="tool_done",
        step=step_idx,
        data={"tool_name": "get_robot_info", "result": robot_obs},
    )

    if robot_obs.get("error"):
        result = WorkflowRunResult(
            mode="workflow_create_jira",
            intent="create_jira",
            steps=steps,
            final_answer=f"无法查询设备信息：{robot_obs['error']}。请确认 SN/DID 是否正确。",
            workflow_data={"success": False, "error": robot_obs["error"]},
        )
        async for event in emit_workflow_finish(payload, result):
            yield event
        return

    sn = sn or robot_obs.get("sn_number") or ""
    did = did or robot_obs.get("device_id") or ""
    if not firmware_version:
        firmware_version = robot_obs.get("firmware_version") or ""
    product_type = robot_obs.get("product_full_name") or ""
    region = "cn" if robot_obs.get("country_code") == "cn" else "ovs"

    if not product_type:
        result = WorkflowRunResult(
            mode="workflow_create_jira",
            intent="create_jira",
            steps=steps,
            final_answer=(
                f"已查到设备（产品：{robot_obs.get('product_name')}），"
                "但该产品暂不支持一键提单，请手动创建 Jira 单。"
            ),
            workflow_data={
                "success": False,
                "error": "unsupported_product",
                "robot_info": robot_obs,
            },
        )
        async for event in emit_workflow_finish(payload, result):
            yield event
        return

    user_email = _get_user_email(context)
    if not user_email:
        result = WorkflowRunResult(
            mode="workflow_create_jira",
            intent="create_jira",
            steps=steps,
            final_answer="无法获取当前用户邮箱，无法创建 Jira 单。请重新登录后重试。",
            workflow_data={"success": False, "error": "missing_user_email"},
        )
        async for event in emit_workflow_finish(payload, result):
            yield event
        return

    attachment_urls = payload.get_attachment_urls(context)
    draft = await build_jira_draft(
        product_type=product_type,
        description=description,
        user_email=user_email,
        sn=sn,
        did=did,
        firmware_version=firmware_version,
        region=region,
        bill_code=context.get("bill_code"),
        issue_time=issue_time or context.get("issue_time"),
        app_version=app_version,
        app_id=app_id,
        resources=attachment_urls,
        robot_info=robot_obs,
        issues_count=merged.get("issues_count"),
        issue_index=merged.get("issue_index"),
    )

    preview_text = format_jira_preview(draft, robot_obs)
    action_id = str(uuid.uuid4())
    action_payload = build_action_required_payload(
        action_type=ACTION_TYPE_CREATE_JIRA,
        draft=draft,
        preview_text=preview_text,
        kind="workflow",
        mode="workflow_create_jira",
        intent="create_jira",
        steps=[
            {
                "step": s.step,
                "thought": s.thought,
                "action": s.action,
                "action_input": s.action_input,
                "observation": s.observation,
            }
            for s in steps
        ],
        action_id=action_id,
        extra={
            "robot_info": {
                "product_name": robot_obs.get("product_name"),
                "firmware_version": firmware_version,
            },
            "issues_count": merged.get("issues_count"),
            "issue_index": merged.get("issue_index"),
            "attachment_count": len(attachment_urls),
            "files": payload.files_for_llm(),
        },
    )
    yield ReactEvent(
        type="action_required",
        step=len(steps) or 1,
        data={
            **action_payload,
            "user_email": user_email,
        },
    )
    return
