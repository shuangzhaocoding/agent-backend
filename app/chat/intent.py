# -*- coding: utf-8 -*-
#
# 用户意图识别（支持 context.intent 强制指定）
#
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal

from common.logger import logger

from chat.llm import DEEPSEEK_MODEL, complete_json
from chat.schemas import AgentChatRequest

# 遗留意图分类（主链路已改为 Skill 路由）；常量保留供 classify_intent 等旧接口使用
INTENT_CLASSIFY_SUFFIX = """
你的任务是意图分类。根据用户最新输入（可结合简短历史）判断意图，只输出 JSON，不要 markdown：

{
  "intent": "create_jira" | "query_product_attrs" | "general",
  "confidence": 0.0-1.0,
  "slots": {
    "description": "问题描述（若有）",
    "sn": "机器人SN（若有）",
    "did": "设备DID（若有）",
    "issue_time": "问题发生时间（若有）"
  }
}

意图定义：
- create_jira：用户要提 Jira 单、建工单、反馈缺陷、一键提单
- query_product_attrs：用户询问产品参数、规格、型号对比
- general：其余云鲸售后专业查询

slots 可从用户文本中识别问题描述、SN、DID、问题发生时间等（若有）。"""

INTENT_CLASSIFY_SYSTEM = (
    "你是云鲸售后助手的意图分类器，只做意图判断，不回答问题。"
    + INTENT_CLASSIFY_SUFFIX
)

IntentType = Literal["create_jira", "query_product_attrs", "general"]

VALID_INTENTS = frozenset({"create_jira", "query_product_attrs", "general"})

JIRA_STRUCTURED_MARKERS = (
    "【问题描述】", "【SN】", "【deviceid", "deviceid", "【固件版本】",
    "【问题发生时间】", "【APP版本】", "【APPID】",
)

JIRA_KEYWORDS = (
    "提单", "提jira", "建jira", "创建jira", "jira单", "jira工单",
    "一键提单", "反馈问题", "建缺陷", "创建工单", "提交工单",
)
ATTRS_KEYWORDS = (
    "参数", "规格", "吸力", "尺寸", "重量", "电池", "续航", "对比",
    "多大", "多重", "容量", "噪音", "尘盒", "水箱",
)
SN_INFO_KEYWORDS = (
    "三码", "四码", "关联sn", "包装sn", "机器码", "基站sn", "充电底座sn",
    "上下水", "关联码",
)
DLOG_KEYWORDS = (
    "dlog", "设备日志", "日志分析", "分析日志", "下载日志", "工作流日志",
)
BAG_KEYWORDS = (
    "bag", "bag开关", "bag上传", "日志上传", "立即上传", "打开bag", "关闭bag",
)
OTA_KEYWORDS = (
    "ota", "升级路线", "升级路径", "固件升级", "升级进度", "能升到", "推送版本",
)
APP_WHITELIST_KEYWORDS = (
    "appid加白", "app加白", "加白", "展厅测试", "kol加白", "appid白名单",
)
NETWORK_DIAGNOSIS_KEYWORDS = (
    "离线诊断", "网络诊断", "配网失败", "wifi诊断", "wifi连接", "ping超时", "mqtt断连",
    "配网记录", "配网历史",
)
SN_PATTERN = re.compile(r"YX[A-Z0-9]{12,}", re.IGNORECASE)
DID_PATTERN = re.compile(r"[a-f0-9]{32}")


@dataclass
class IntentResult:
    intent: IntentType
    confidence: float = 1.0
    source: str = "model"
    slots: dict[str, Any] = field(default_factory=dict)


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


def _has_structured_jira_text(text: str) -> bool:
    return any(marker in (text or "") for marker in JIRA_STRUCTURED_MARKERS)


def _has_technical_signal(text: str) -> bool:
    lowered = (text or "").lower()
    if _has_structured_jira_text(text):
        return True
    keyword_groups = (
        JIRA_KEYWORDS,
        ATTRS_KEYWORDS,
        SN_INFO_KEYWORDS,
        DLOG_KEYWORDS,
        BAG_KEYWORDS,
        OTA_KEYWORDS,
        APP_WHITELIST_KEYWORDS,
        NETWORK_DIAGNOSIS_KEYWORDS,
    )
    for group in keyword_groups:
        if any(kw in lowered or kw in text for kw in group):
            return True
    if SN_PATTERN.search(text or ""):
        return True
    if DID_PATTERN.search(lowered):
        return True
    return False


def _rule_based_intent(text: str) -> IntentResult | None:
    lowered = (text or "").lower()
    structured_jira = _has_structured_jira_text(text)

    jira_hit = any(kw in lowered or kw in text for kw in JIRA_KEYWORDS)
    attrs_hit = any(kw in text for kw in ATTRS_KEYWORDS)
    sn_info_hit = any(kw in lowered or kw in text for kw in SN_INFO_KEYWORDS)
    dlog_hit = any(kw in lowered or kw in text for kw in DLOG_KEYWORDS)
    bag_hit = any(kw in lowered or kw in text for kw in BAG_KEYWORDS)
    ota_hit = any(kw in lowered or kw in text for kw in OTA_KEYWORDS)
    app_whitelist_hit = any(kw in lowered or kw in text for kw in APP_WHITELIST_KEYWORDS)
    network_diag_hit = any(kw in lowered or kw in text for kw in NETWORK_DIAGNOSIS_KEYWORDS)

    if jira_hit or structured_jira:
        return IntentResult(
            intent="create_jira",
            confidence=0.9 if structured_jira else 0.85,
            source="rule",
            slots={},
        )
    if sn_info_hit and not jira_hit:
        return IntentResult(
            intent="general",
            confidence=0.9,
            source="rule",
            slots={},
        )
    if dlog_hit and not jira_hit:
        return IntentResult(
            intent="general",
            confidence=0.9,
            source="rule",
            slots={},
        )
    if bag_hit and not jira_hit:
        return IntentResult(
            intent="general",
            confidence=0.9,
            source="rule",
            slots={},
        )
    if ota_hit and not jira_hit:
        return IntentResult(
            intent="general",
            confidence=0.9,
            source="rule",
            slots={},
        )
    if app_whitelist_hit and not jira_hit:
        return IntentResult(
            intent="general",
            confidence=0.9,
            source="rule",
            slots={},
        )
    if network_diag_hit and not jira_hit:
        return IntentResult(
            intent="general",
            confidence=0.9,
            source="rule",
            slots={},
        )
    if attrs_hit and not jira_hit:
        return IntentResult(
            intent="query_product_attrs",
            confidence=0.8,
            source="rule",
            slots={},
        )
    return None


def _merge_slots(
    text: str,
    context: dict[str, Any] | None,
    model_slots: dict[str, Any] | None,
) -> dict[str, Any]:
    slots: dict[str, Any] = {}
    if model_slots:
        slots.update({k: v for k, v in model_slots.items() if v})
    if context:
        for key in (
            "sn", "did", "description", "product_type",
            "issue_time", "firmware_version", "app_version", "app_id",
            "region", "product", "products",
        ):
            if context.get(key):
                slots[key] = context[key]
    return slots


INTENT_DISPLAY_LABELS: dict[str, str] = {
    "create_jira": "创建 Jira 工单",
    "general": "售后查询",
    "query_product_attrs": "产品参数",
}

INTENT_SOURCE_LABELS: dict[str, str] = {
    "context": "指定",
    "rule": "规则",
    "model": "模型",
    "local_model": "本地模型",
    "fallback": "兜底",
}


def build_intent_event_data(
    result: IntentResult,
    *,
    elapsed_ms: float | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "intent": result.intent,
        "intent_label": INTENT_DISPLAY_LABELS.get(result.intent, result.intent),
        "confidence": result.confidence,
        "source": result.source,
        "source_label": INTENT_SOURCE_LABELS.get(result.source, result.source),
        "slots": result.slots or {},
    }
    if elapsed_ms is not None:
        data["elapsed_ms"] = round(elapsed_ms, 1)
    return data


async def _classify_intent_impl(payload: AgentChatRequest) -> IntentResult:
    context = payload.context or {}
    user_text = payload.get_current_user_content()

    forced = context.get("intent")
    if forced and forced in VALID_INTENTS:
        return IntentResult(
            intent=forced,
            confidence=1.0,
            source="context",
            slots=_merge_slots(user_text, context, None),
        )

    rule_result = _rule_based_intent(user_text)
    if rule_result and rule_result.confidence >= 0.85:
        rule_result.slots = _merge_slots(user_text, context, rule_result.slots)
        return rule_result

    history = payload.get_history_messages()
    history_text = ""
    if history:
        recent = history[-2:]
        history_text = "\n".join(f"{m['role']}: {m['content']}" for m in recent)

    user_content = user_text
    if history_text:
        user_content = f"历史对话：\n{history_text}\n\n当前用户输入：{user_text}"

    try:
        response = await complete_json(
            messages=[
                {"role": "system", "content": INTENT_CLASSIFY_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            temperature=0.1,
            model=DEEPSEEK_MODEL,
        )
        parsed = _parse_ai_json(response.choices[0].message.content or "")
        intent = parsed.get("intent") or "general"
        if intent not in VALID_INTENTS:
            intent = "general"
        confidence = float(parsed.get("confidence") or 0.5)
        model_slots = parsed.get("slots") if isinstance(parsed.get("slots"), dict) else {}
        slots = _merge_slots(user_text, context, model_slots)
        return IntentResult(intent=intent, confidence=confidence, source="model", slots=slots)
    except Exception:
        logger.error("意图分类失败", exc_info=True)
        if rule_result:
            rule_result.slots = _merge_slots(user_text, context, rule_result.slots)
            return rule_result
        return IntentResult(
            intent="general",
            confidence=0.3,
            source="fallback",
            slots=_merge_slots(user_text, context, None),
        )


async def classify_intent(payload: AgentChatRequest) -> IntentResult:
    return await _classify_intent_impl(payload)


async def iter_classify_intent(
    payload: AgentChatRequest,
) -> AsyncIterator[tuple[Literal["classifying", "classified"], Any]]:
    """先产出 classifying，分类完成后再产出 classified 及事件数据。"""
    yield "classifying", None
    t0 = time.perf_counter()
    result = await _classify_intent_impl(payload)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    yield "classified", (result, build_intent_event_data(result, elapsed_ms=elapsed_ms))
