# -*- coding: utf-8 -*-
#
# Jira 建单服务
#
from typing import Any, Optional

from async_outer_apis import JiraSystem
from common.logger import logger
from product_config import ProductConfigManager, get_product_name_by_unique_id

from agent.router import (
    generate_jira_summary_by_deepseek,
    product_to_jira_project,
)

async def create_jira_issue(
    product_type: str,
    description: str,
    user_email: str,
    sn: Optional[str] = None,
    did: Optional[str] = None,
    firmware_version: Optional[str] = None,
    region: Optional[str] = "cn",
    bill_code: Optional[str] = None,
    issue_time: Optional[str] = None,
    app_version: Optional[str] = None,
    app_id: Optional[str] = None,
    resources: Optional[list[str]] = None,
) -> dict[str, Any]:
    """创建 Jira 单，返回结构化结果。"""
    project_key = product_to_jira_project.get(product_type, {}).get("project_key")
    # project_key = "YTL"
    product_model = product_to_jira_project.get(product_type, {}).get("product_model")
    if not project_key:
        return {
            "success": False,
            "error": f"此产品型号「{product_type}」暂不支持一键提单，请手动创建 Jira 单",
        }

    soc_platform = product_to_jira_project.get(product_type, {}).get("soc_platform") or "无选项"
    summary = await generate_jira_summary_by_deepseek(description)
    labels = ["市场问题专项"]
    if region == "ovs":
        labels = ["海外市场问题专项"]
    elif product_type in ("逍遥 002 Max", "逍遥 002 Max 超薄全能基站"):
        labels = ["市场问题专项(CX2/7)"]

    resources_text = "\n".join(resources or [])
    formatted_description = f"""
    【问题描述】：{description}
    【售后工单号】：{bill_code}
    【SN】：{sn}
    【DID】：{did}
    【机器人版本】：{firmware_version}
    【问题发生时间】：{issue_time}
    【APP版本】：{app_version}
    【APPID】：{app_id}
    【图片视频资源】：{resources_text}
    【AI初步分析结果】：\n无
    """
    payload = {
        "product_type": product_type,
        "summary": summary,
        "description": formatted_description,
        "assignee": user_email,
        "reporter": user_email,
        "priority": "A级",
        "labels": labels,
        "issuetype": "售后反馈/Aftersales Feedback",
        "issue_source": "FAE",
        "issue_belong": "机器人",
        "soc_platform": soc_platform,
        "frequency": "偶现bug",
        "project_key": project_key,
        "product_model": product_model,
        "sn": sn,
        "did": did,
        "firmware_version": firmware_version,
        "bill_code": bill_code,
        "issue_time": issue_time,
        "resources": resources or [],
    }

    jira = JiraSystem()
    result = await jira.create_issue_by_product_model(payload)

    if not result:
        logger.error(
            f"Jira 建单无响应: product_type={product_type} project_key={project_key}"
        )
        return {"success": False, "error": "Jira 建单失败，请稍后重试"}

    if result.get("errorMessages") or result.get("errors"):
        errors = result.get("errorMessages") or result.get("errors")
        return {"success": False, "error": f"Jira 建单失败: {errors}"}

    return {
        "success": True,
        "key": result.get("key"),
        "id": result.get("id"),
        "jira_url": result.get("jira_url"),
        "summary": summary,
        "product_type": product_type,
        "sn": sn,
        "did": did,
        "firmware_version": firmware_version,
    }
