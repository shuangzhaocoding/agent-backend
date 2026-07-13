# -*- coding: utf-8 -*-
#
# 使用大模型从用户文本提取 Jira 建单字段（不做正则匹配）
#
import json
import re
from typing import Any

from common.logger import logger

from chat.llm import DEEPSEEK_MODEL, complete_json

JIRA_EXTRACT_SYSTEM = """你是云鲸售后 Jira 提单信息抽取助手。从用户输入中识别并提取结构化字段。

输入可能是自由描述，也可能是如下格式（可能出现多条，用空行分隔）：

格式一（【标签】）：
【问题描述】：鲸灵模式下机器人识别到液体脏污避开了未清洁
【问题发生时间】：2026-06-10 10:00:43
【deviceid 】：4cf5203e7bab473bb92121e898b2ff97
【SN】：YXCAAM2624XB05N0347
【固件版本】：v01.08.01.00
【APP版本】：v2.6.90
【APPID】：15862199

格式二（自由文本，问题描述 + SN + 时间）：
基站溢水，重启无效 YXCAAM2629XB05N0097 时间：2026-06-04 03:01:11.035

回洗拖布后返回途中拖布一直工作中，应该是抬起拖布，到拖地点再放下拖布
YFEAAM2593XB00C0098 2026-06-16 03:01:11.035

只输出 JSON，不要 markdown 代码块，不要额外解释：

{
  "issues": [
    {
      "description": "问题描述全文",
      "issue_time": "问题发生时间，无则空字符串",
      "did": "设备 ID（deviceid/DID/设备ID，一般为32位十六进制）",
      "sn": "机器人 SN",
      "firmware_version": "固件版本",
      "app_version": "APP 版本",
      "app_id": "APPID"
    }
  ]
}

规则：
1. 文本中包含多条独立问题时，按出现顺序分别放入 issues，每条对应一组字段
2. 某字段在原文中不存在则输出空字符串，禁止编造
3. did 兼容 deviceid、DID、device id、设备ID 等写法（含【deviceid 】等带空格标签）
4. sn 兼容 SN、sn 等写法；时间兼容「时间：」「问题发生时间」或 SN 后的日期时间
5. description 只保留问题描述内容，不要带上 SN、时间行及其他标签行
6. 只输出 JSON"""


def _parse_ai_json(text: str) -> dict[str, Any]:
    content = (text or "").strip()
    if not content:
        return {}
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                return {}
    return {}


def _normalize_issue(raw: dict[str, Any] | None) -> dict[str, str]:
    raw = raw or {}
    return {
        "description": str(raw.get("description") or "").strip(),
        "issue_time": str(raw.get("issue_time") or "").strip(),
        "did": str(raw.get("did") or "").strip(),
        "sn": str(raw.get("sn") or "").strip(),
        "firmware_version": str(raw.get("firmware_version") or "").strip(),
        "app_version": str(raw.get("app_version") or "").strip(),
        "app_id": str(raw.get("app_id") or "").strip(),
    }


async def extract_jira_fields_by_llm(user_text: str) -> dict[str, Any]:
    """调用快模型抽取建单字段，返回 { issues: [...], error?: str }。"""
    text = (user_text or "").strip()
    if not text:
        return {"issues": [], "error": "empty_input"}

    try:
        response = await complete_json(
            messages=[
                {"role": "system", "content": JIRA_EXTRACT_SYSTEM},
                {"role": "user", "content": text},
            ],
            temperature=0.1,
            model=DEEPSEEK_MODEL,
        )
        parsed = _parse_ai_json(response.choices[0].message.content or "")
        raw_issues = parsed.get("issues")
        if not isinstance(raw_issues, list):
            raw_issues = [parsed] if parsed.get("description") or parsed.get("sn") else []
        issues = [_normalize_issue(item) for item in raw_issues if isinstance(item, dict)]
        logger.debug(f"Jira 字段抽取 issues_count={len(issues)}")
        return {"issues": issues}
    except Exception:
        logger.error("Jira 字段 LLM 抽取失败", exc_info=True)
        return {"issues": [], "error": "llm_extract_failed"}


def merge_jira_issue_fields(
    llm_result: dict[str, Any],
    context: dict[str, Any] | None = None,
    slots: dict[str, Any] | None = None,
    issue_index: int = 0,
) -> dict[str, Any]:
    """合并 LLM 抽取、context、意图 slots；返回单条 issue + 元信息。"""
    context = context or {}
    slots = slots or {}
    issues = llm_result.get("issues") or []

    idx = issue_index
    if idx < 0 or idx >= len(issues):
        idx = 0

    issue = _normalize_issue(issues[idx] if issues else {})

    slot_map = {
        "description": slots.get("description"),
        "issue_time": slots.get("issue_time"),
        "did": slots.get("did"),
        "sn": slots.get("sn"),
        "firmware_version": slots.get("firmware_version"),
        "app_version": slots.get("app_version"),
        "app_id": slots.get("app_id"),
    }
    for key, value in slot_map.items():
        if value:
            issue[key] = str(value).strip()

    context_map = {
        "description": context.get("description"),
        "issue_time": context.get("issue_time"),
        "did": context.get("did"),
        "sn": context.get("sn"),
        "firmware_version": context.get("firmware_version"),
        "app_version": context.get("app_version"),
        "app_id": context.get("app_id"),
    }
    for key, value in context_map.items():
        if value:
            issue[key] = str(value).strip()

    return {
        "issue": issue,
        "issues_count": len(issues),
        "issue_index": idx,
        "extract_error": llm_result.get("error"),
    }
