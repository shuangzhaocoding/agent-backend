# -*- coding: utf-8 -*-
#
# Agent 工具注册与执行
#
from datetime import date, datetime, timedelta
import json
from typing import Any, Callable, Awaitable

import pytz

from as_improve_module.dlog_invoking import get_dlog_datas
from async_outer_apis.door_guard_api import DoorGuardAPI
from device_module.router import (
    device_network_diagnosis,
    get_bag_upload_status_api,
    get_nw_config_historys,
    get_robot_info_api,
    get_sn_info_api,
    push_firmware_api,
    robot_ota_router,
    switch_bag_upload_api,
)
from j1_module.router import get_sn_info_api_j1
from j2_module.router import get_sn_info_api_j2
from setting_module.router import get_firmware_infos
from models.db_model import AppAddWhiteRecordModel
from schema import CodeEnum, DlogResult
from utils import download_logs, get_logs_dir

from chat.tool_registry import ToolRegistry, register_from_specs
from chat.product_attrs.service import (
    build_alias_map,
    canonicalize_product_names,
    format_field_descriptions,
    format_product_alias_mapping,
    get_attrs_by_names,
    get_scan_attrs_by_fields,
    list_attr_field_catalog,
    list_product_catalog,
    normalize_region,
    subset_attrs_by_fields,
)

ToolHandler = Callable[..., Awaitable[dict[str, Any]] | dict[str, Any]]

# DEFAULT_DLOG_COMMANDS = [
#     "", "show", "all", "fan", "para", "ba", "dis", "com", "sta",
#     "contact", "clean", "wlan", "asr", "dirty", "task",
# ]
DEFAULT_DLOG_COMMANDS = [
    "asr"
]
DEFAULT_DLOG_LOOKBACK_DAYS = 3
DEFAULT_NETWORK_DIAGNOSIS_DAYS = 7

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "search_knowledge",
        "description": (
            "检索售后知识库，适用于故障现象、排查步骤、使用说明等产品知识问题。"
            "用户提到 16 进制系统错误码（如 0x02100018 或 0x2100018）时，query 中须保留完整错误码以便精确匹配。"
            "返回的 hits/summary 可能含图片（Markdown ![]()）、视频（<video> 标签）、文档链接，回答时须完整保留。"
        ),
        "parameters": {
            "query": "检索关键词或问题描述；含错误码时保留 0x 开头的 16 进制码（7/8 位，如 0x02100018、0x2100018）",
        },
    },
    {
        "name": "list_product_catalog",
        "description": (
            "列出产品参数库中的全部型号目录（规范 product_name 与 aliases 别名）。"
            "不确定型号、需将用户简称映射为规范型号、或对比前确认有哪些机型时先调用。"
        ),
        "parameters": {
            "region": {
                "type": "string",
                "description": "区域 cn（国内）或 ovs（海外），留空表示不按区域筛选",
            },
        },
        "required": [],
    },
    {
        "name": "list_attr_fields",
        "description": (
            "列出产品参数字段模板（field_key、中文 label、分组、说明）。"
            "查询具体参数项、全库筛选（如吸力大于某值）前先调用以确认 field_key。"
        ),
        "parameters": {
            "region": {
                "type": "string",
                "description": "区域 cn 或 ovs，留空表示不按区域筛选",
            },
        },
        "required": [],
    },
    {
        "name": "get_product_attrs",
        "description": (
            "按规范 product_name 查询一款或多款产品的参数。"
            "对比、单参查询时使用；product_names 须为目录中的规范型号或可先 list_product_catalog 确认。"
            "可传 field_keys 仅返回指定字段以节省上下文。"
        ),
        "parameters": {
            "product_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "规范产品型号列表，最多 3 个；可用别名但建议先查目录",
            },
            "region": {
                "type": "string",
                "description": "区域 cn 或 ovs，留空表示不按区域筛选",
            },
            "field_keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选，仅返回这些 field_key 对应参数；不传则返回全部字段",
            },
        },
        "required": ["product_names"],
    },
    {
        "name": "scan_product_attrs",
        "description": (
            "全库扫描：拉取所有产品在指定字段上的参数值。"
            "用于未指定型号的条件筛选、排序（如吸力大于 20000Pa 的机型有哪些）。"
            "须先 list_attr_fields 确认 field_key，再传入 field_keys。"
        ),
        "parameters": {
            "field_keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要扫描的 field_key 列表，如 max_suction、battery_life",
            },
            "region": {
                "type": "string",
                "description": "区域 cn 或 ovs，留空表示不按区域筛选",
            },
        },
        "required": ["field_keys"],
    },
    {
        "name": "get_robot_info",
        "description": (
            "根据设备 SN 码或 DID 从 AIOT 平台查询单台设备信息，"
            "包括产品型号、固件版本、激活状态、在线状态、国家/数据中心、OTA 升级情况等。"
            "用户提供 SN/DID 并询问该设备状态、版本、激活、在线等信息时使用。"
            "SN 示例：YXCAAM263HXB05N1105、YXCAAM262QXB05N0481；"
            "DID 示例：82eb290783b2480384e74d6f13755e78、5f96d3cbd2044f4a8a0fd5f91136b72e。"
        ),
        "parameters": {
            "did_or_sn": (
                "设备 SN 码或 DID（设备 ID）。"
                "SN 为字母数字组合（如 YXCAAM263HXB05N1105）；"
                "DID 为 32 位十六进制字符串（如 82eb290783b2480384e74d6f13755e78）"
            ),
        },
    },
    {
        "name": "get_ota_router",
        "description": (
            "从 AIOT 平台查询设备 OTA 固件升级路线，包含各版本升级路径、目标固件版本、"
            "升级状态（如待触发、升级中、升级成功等）。"
            "用户询问升级路线、OTA 路径、能升到哪个版本、升级进度时使用。"
            "SN 示例：YXCAAM263HXB05N1105；DID 示例：82eb290783b2480384e74d6f13755e78。"
        ),
        "parameters": {
            "did_or_sn": (
                "设备 SN 码或 DID（设备 ID）。"
                "SN 为字母数字组合；DID 为 32 位十六进制字符串"
            ),
        },
    },
    {
        "name": "get_firmware_infos",
        "description": (
            "查询固件版本库，获取某机型在指定国家/地区最新可推送的市场版本及历史版本列表。"
            "用户询问某型号最新固件、市场版本、可推送版本、固件推送配置时使用；"
            "需已知产品型号（如 J4、J5）与国家码（如 cn、us、jp，默认 cn）。"
            "市场版本指 allowed_push=true 且 is_for_outside=true 的对外发布版本。"
        ),
        "parameters": {
            "product": "产品型号，如 J4、J5、逍遥 002",
            "country": {
                "type": "string",
                "description": "机器国家码，如 cn（中国）、us（美国）、jp（日本）；默认 cn",
            },
            "firmware_version": {
                "type": "string",
                "description": "可选，按版本号精确筛选",
            },
            "firmware_id": {
                "type": "string",
                "description": "可选，按 AIOT 固件推送 ID 精确筛选",
            },
        },
        "required": ["product"],
    },
    {
        "name": "push_firmware",
        "description": (
            "在 AIOT 平台向单台设备推送指定固件版本（单点 OTA 升级）。"
            "用户要求推送固件、升级设备到某版本、单点下发 OTA 时使用；"
            "执行前须由用户在前端确认。"
            "推送前建议先 get_robot_info 确认设备信息，并用 get_firmware_infos 获取可推送版本的 firmware_id；"
            "firmware_id 须来自固件版本库且 allowed_push=true。"
            "SN 示例：YXCAAM263HXB05N1105；DID 示例：82eb290783b2480384e74d6f13755e78。"
        ),
        "parameters": {
            "did_or_sn": (
                "设备 SN 码或 DID（设备 ID）。"
                "SN 为字母数字组合；DID 为 32 位十六进制字符串"
            ),
            "firmware_id": {
                "type": "string",
                "description": "AIOT 固件推送 ID（来自 get_firmware_infos 返回的 firmware_id）",
            },
        },
        "required": ["did_or_sn", "firmware_id"],
    },
    {
        "name": "get_sn_info",
        "description": (
            "根据任意一台关联 SN 从 MES 查询 J3 及以后新款设备的三码/四码等关联 SN 信息，"
            "包括包装 SN、机器人 SN、基站 SN、充电底座 SN、上下水模块 SN、手柄 SN、洗地机整机 SN 等。"
            "J1/J2 三码请用 get_sn_info_j1 / get_sn_info_j2。"
            "SN 示例：YXCAAM263HXB05N1105、YXCAAM262QXB05N0481。"
        ),
        "parameters": {
            "sn_number": (
                "设备 SN 码（以 Y 开头的 19 位字母数字，如 YXCAAM263HXB05N1105）。"
                "可用包装 SN、机器人 SN、基站 SN 等任意一个关联码查询"
            ),
        },
    },
    {
        "name": "get_sn_info_j1",
        "description": (
            "查询 J1 产品三码信息（SN 系统），返回机器人 SN、基站 SN、包装 SN、机器人 CID。"
            "用户询问 J1、云鲸 J1、T10 等老款机型三码时使用；不要用 get_sn_info（MES）。"
            "SN 为以 Y 开头的 19 位字母数字。"
        ),
        "parameters": {
            "sn_number": (
                "J1 设备 SN 码（以 Y 开头的 19 位字母数字）。"
                "可用包装 SN、机器人 SN、基站 SN 等任意一个关联码查询"
            ),
        },
    },
    {
        "name": "get_sn_info_j2",
        "description": (
            "查询 J2 产品三码信息（SN 系统），返回机器人 SN、基站 SN、包装 SN。"
            "用户询问 J2、云鲸 J2 等机型三码时使用；不要用 get_sn_info（MES）。"
            "SN 为以 Y 开头的 19 位字母数字。"
        ),
        "parameters": {
            "sn_number": (
                "J2 设备 SN 码（以 Y 开头的 19 位字母数字）。"
                "可用包装 SN、机器人 SN、基站 SN 等任意一个关联码查询"
            ),
        },
    },
    {
        "name": "get_bag_upload_status",
        "description": (
            "从 AIOT 平台查询设备 Bag 日志上传开关状态（是否开启立即上传）。"
            "用户询问 Bag 开关、Bag 上传状态、日志上传是否打开时使用。"
            "SN 示例：YXCAAM263HXB05N1105；DID 示例：82eb290783b2480384e74d6f13755e78。"
        ),
        "parameters": {
            "did_or_sn": (
                "设备 SN 码或 DID（设备 ID）。"
                "SN 为字母数字组合；DID 为 32 位十六进制字符串"
            ),
        },
    },
    {
        "name": "switch_bag_upload",
        "description": (
            "在 AIOT 平台打开或关闭设备 Bag 日志立即上传开关。"
            "用户要求打开/关闭 Bag 上传、开启/关闭日志上传时使用；"
            "查询当前状态请用 get_bag_upload_status。"
            "SN 示例：YXCAAM263HXB05N1105；DID 示例：82eb290783b2480384e74d6f13755e78。"
        ),
        "parameters": {
            "did_or_sn": (
                "设备 SN 码或 DID（设备 ID）。"
                "SN 为字母数字组合；DID 为 32 位十六进制字符串"
            ),
            "upload_status": {
                "type": "boolean",
                "description": "true 打开立即上传，false 关闭立即上传",
            },
        },
        "required": ["did_or_sn", "upload_status"],
    },
    {
        "name": "add_users_to_app",
        "description": (
            "给 APPID 加白，开通 KOL/展厅测试员权限（DoorGuard 批量加用户）。"
            "用户要求 APPID 加白、开通展厅测试权限、KOL 用户加白时使用。"
            "每行一个 APPID，格式为纯数字 APPID 或「APPID 备注名」。"
            "示例：1234567890 或 1234567890 张三"
        ),
        "parameters": {
            "appid": (
                "APPID 列表，多行文本；每行一个 APPID（纯数字），"
                "可在同一行 APPID 后加备注名，如「1234567890 张三」"
            ),
        },
    },
    {
        "name": "device_network_diagnosis",
        "description": (
            "查询设备离线/网络诊断埋点数据，分析 WiFi 硬件、驱动、AP 连接、外网、DNS、"
            "云鲸服务连接等环节的诊断结果（含机器人端与 APP 端上报）。"
            "用户询问离线诊断、网络诊断、配网失败原因、WiFi 连接问题时使用。"
            "SN 示例：YXCAAM263HXB05N1105；DID 示例：82eb290783b2480384e74d6f13755e78。"
        ),
        "parameters": {
            "did_or_sn": (
                "设备 SN 码或 DID（设备 ID）。"
                "SN 为字母数字组合；DID 为 32 位十六进制字符串"
            ),
            "start_time": "诊断开始时间 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS，默认近 7 天 00:00:00",
            "end_time": "诊断结束时间 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS，默认当前时间",
        },
        "required": ["did_or_sn"],
    },
    {
        "name": "get_nw_config_history",
        "description": (
            "从鲸巢平台查询设备 BLE 配网记录历史（按时间倒序）。"
            "用户询问配网记录、配网历史、连网失败记录时使用。"
            "key 可为 DID、SN、用户 uid 或 uuid。"
            "SN 示例：YXCAAM263HXB05N1105；DID 示例：82eb290783b2480384e74d6f13755e78。"
        ),
        "parameters": {
            "key": (
                "查询键：设备 SN、DID、用户 uid 或 uuid（与鲸巢埋点表字段匹配）"
            ),
        },
    },
    {
        "name": "get_dlog_data",
        "description": (
            "下载并分析设备 Dlog 日志（简单工作流、电量、风机、通信、任务等）。"
            "用户要求分析设备日志、查看 dlog、排查日志中的故障现象时使用。"
            "需设备 DID 或 SN，以及日志时间范围（默认近 7 天）。"
            "DID 示例：82eb290783b2480384e74d6f13755e78；"
            "SN 示例：YXCAAM263HXB05N1105。"
        ),
        "parameters": {
            "did_or_sn": (
                "设备 DID 或 SN。"
                "DID 为 32 位十六进制；SN 为字母数字（如 YXCAAM263HXB05N1105）"
            ),
            "start_date": "日志开始日期 YYYY-MM-DD，默认近 7 天",
            "end_date": "日志结束日期 YYYY-MM-DD，默认今天",
        },
        "required": ["did_or_sn"],
    },
    {
        "name": "get_app_logs",
        "description": (
            "获取云鲸 APP 客户端日志，用于排查配网失败、连网异常、APP 侧报错等。"
            "需提供 APP 用户 ID（8 位 uid）或用户 uuid（32 位），以及时间范围（默认近 7 天）。"
            "日志打包后上传 OBS，返回可点击下载的 OBS 链接及预览片段。"
            "可与 device_network_diagnosis（设备端）配合分析网络问题。"
        ),
        "parameters": {
            "user_account": (
                "APP 用户标识：8 位数字用户 ID（uid），或 32 位小写字母数字 uuid；"
                "不是手机号"
            ),
            "region": (
                "区域 cn（国内）或 us（海外），默认 cn"
            ),
            "start_time": "起始时间 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS，默认近 7 天 00:00:00",
            "end_time": "结束时间 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS，默认当前时间",
        },
        "required": ["user_account"],
    },
    {
        "name": "get_error_code_info",
        "description": (
            "从 error_code_info 表查询错误码详情与 APP UI 错误码映射。"
            "支持十六进制系统错误码（如 0x02100018、0x2100018）"
            "或 4 位纯数字 APP UI 错误码（如 2003）；"
            "UI 错误码会先映射为对应十六进制 code，再可用于 search_knowledge 检索 SOP。"
        ),
        "parameters": {
            "code": (
                "错误码：0x 开头的十六进制系统错误码（7/8 位），"
                "或 4 位纯数字 APP UI 错误码（appCode）"
            ),
        },
        "required": ["code"],
    },
    {
        "name": "get_voice_code_info",
        "description": (
            "从 voice_code_info 表查询语音错误码详情（机器人播报的语音文案、关联系统错误码等）。"
            "支持 voice 开头的语音码（如 voice451、voiceK16），"
            "或关联的十六进制系统错误码（如 0x02100018），"
            "或 4 位 APP UI 错误码（如 2003）。"
            "纯数字可尝试补全为 voice{数字}（如 451 → voice451）。"
        ),
        "parameters": {
            "code": (
                "语音错误码或关联码：voice 开头语音码（voice451）、"
                "十六进制系统错误码（0x02100018）、或 4 位 APP UI 错误码（2003）"
            ),
        },
        "required": ["code"],
    },
    {
        "name": "get_current_time",
        "description": (
            "获取当前日期与时间（北京时间 Asia/Shanghai，UTC+8）。"
            "用户询问现在几点、今天日期，或需推算日志/诊断查询的默认时间范围时调用。"
        ),
        "parameters": {},
        "required": [],
    },
]

register_from_specs(TOOL_SPECS)

# 不绑定单一 Skill，任意工具集加载时均附带
ALWAYS_AVAILABLE_TOOLS = frozenset({
    "get_current_time",
    "get_error_code_info",
    "get_voice_code_info",
})

FINISH_ACTION = "finish"


def build_openai_tools(skill_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """转为 OpenAI tools 定义；skill_ids 为空则返回全部已注册工具。"""
    if skill_ids is None:
        return ToolRegistry.to_openai_tools()
    from chat.skills.loader import resolve_tool_names_for_skills

    tool_names = resolve_tool_names_for_skills(skill_ids) | ALWAYS_AVAILABLE_TOOLS
    return ToolRegistry.to_openai_tools(tool_names or None)


def _parse_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except ValueError:
            pass
    if "," in text:
        return [part.strip() for part in text.split(",") if part.strip()]
    return [text]


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in ("true", "1", "yes", "on", "开", "打开", "开启"):
        return True
    if text in ("false", "0", "no", "off", "关", "关闭"):
        return False
    return None


def _resolve_tool_region(action_input: dict[str, Any], context: dict[str, Any]) -> str:
    explicit = str(action_input.get("region") or "").strip()
    if explicit:
        return normalize_region(explicit)
    return normalize_region(context.get("region"))


def _normalize_dlog_result(raw: dict[str, Any]) -> dict[str, Any]:
    data = dict(raw)
    data["f"] = data.pop("", "无简单工作流信息") or "无简单工作流信息"
    if not data.get("all"):
        data["all"] = "无详细工作流信息"
    if not data.get("ba"):
        data["ba"] = "无电量变化信息"
    return data


def _parse_dlog_date(value: str | None, default: date) -> str:
    text = (value or "").strip()
    if not text:
        return default.strftime("%Y-%m-%d")
    return text


def _parse_diagnosis_datetime(
    value: str | None,
    default: datetime,
    *,
    end_of_day: bool = False,
) -> datetime:
    text = (value or "").strip()
    if not text:
        return default
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt == "%Y-%m-%d":
                if end_of_day:
                    return parsed.replace(hour=23, minute=59, second=59)
                return parsed.replace(hour=0, minute=0, second=0)
            return parsed
        except ValueError:
            continue
    return default


async def tool_search_knowledge(
    query: str,
    context: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    from chat.knowledge_store import search_knowledge_with_summary

    tool_context = context or {}
    result = await search_knowledge_with_summary(
        query,
        context=tool_context,
        history_messages=tool_context.get("history_messages"),
    )
    return {
        "tool": "search_knowledge",
        **result,
    }


async def tool_list_product_catalog(
    region: str = "",
    context: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    norm_region = normalize_region(region) if region else _resolve_tool_region({}, context or {})
    catalog = await list_product_catalog(norm_region)
    return {
        "tool": "list_product_catalog",
        "region": norm_region,
        "count": len(catalog),
        "catalog": catalog,
    }


async def tool_list_attr_fields(
    region: str = "",
    context: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    norm_region = normalize_region(region) if region else _resolve_tool_region({}, context or {})
    fields = await list_attr_field_catalog(norm_region)
    return {
        "tool": "list_attr_fields",
        "region": norm_region,
        "count": len(fields),
        "fields": fields,
    }


async def tool_get_product_attrs(
    product_names: Any = None,
    region: str = "",
    field_keys: Any = None,
    context: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    names = _parse_string_list(product_names)
    if not names:
        return {"tool": "get_product_attrs", "error": "请提供 product_names"}

    if len(names) > 3:
        names = names[:3]

    norm_region = normalize_region(region) if region else _resolve_tool_region({}, context or {})
    catalog = await list_product_catalog(norm_region)
    if not catalog:
        return {
            "tool": "get_product_attrs",
            "region": norm_region,
            "error": "无可用产品参数数据",
        }

    alias_map = build_alias_map(catalog)
    canonical_names = canonicalize_product_names(names, alias_map)
    raw_attrs = await get_attrs_by_names(norm_region, canonical_names)
    missing = [name for name in canonical_names if name not in raw_attrs]

    keys = _parse_string_list(field_keys)
    attrs: dict[str, Any] = raw_attrs
    field_desc = ""
    if keys:
        field_catalog = await list_attr_field_catalog(norm_region)
        field_meta = {str(f["field_key"]): f for f in field_catalog}
        attrs = {
            name: subset_attrs_by_fields(raw_attrs.get(name), keys, field_meta)
            for name in canonical_names
            if name in raw_attrs
        }
        field_desc = format_field_descriptions(keys, field_meta)

    alias_mapping = format_product_alias_mapping(catalog, canonical_names)

    return {
        "tool": "get_product_attrs",
        "region": norm_region,
        "products": canonical_names,
        "field_keys": keys or None,
        "field_descriptions": field_desc or None,
        "alias_mapping": alias_mapping,
        "attrs": attrs,
        "missing_products": missing,
    }


async def tool_scan_product_attrs(
    field_keys: Any = None,
    region: str = "",
    context: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    keys = _parse_string_list(field_keys)
    if not keys:
        return {"tool": "scan_product_attrs", "error": "请提供 field_keys"}

    norm_region = normalize_region(region) if region else _resolve_tool_region({}, context or {})
    field_catalog = await list_attr_field_catalog(norm_region)
    if not field_catalog:
        return {
            "tool": "scan_product_attrs",
            "region": norm_region,
            "error": "无可用字段模板",
        }

    field_meta = {str(f["field_key"]): f for f in field_catalog}
    attrs = await get_scan_attrs_by_fields(norm_region, keys, field_meta)
    product_names = list(attrs.keys())

    return {
        "tool": "scan_product_attrs",
        "region": norm_region,
        "field_keys": keys,
        "field_descriptions": format_field_descriptions(keys, field_meta),
        "product_count": len(product_names),
        "attrs": attrs,
    }


async def tool_get_robot_info(did_or_sn: str = "", **_: Any) -> dict[str, Any]:
    key = (did_or_sn or "").strip()
    if not key:
        return {"tool": "get_robot_info", "error": "请提供设备 SN 或 DID"}

    try:
        robot_response = await get_robot_info_api(did_or_sn=key)
    except Exception as exc:
        return {"tool": "get_robot_info", "did_or_sn": key, "error": str(exc)}

    if robot_response and robot_response.code == CodeEnum.SUCCESS:
        return {
            "tool": "get_robot_info",
            "did_or_sn": key,
            "data": robot_response.data or {},
        }

    return {
        "tool": "get_robot_info",
        "did_or_sn": key,
        "error": getattr(robot_response, "message", None) or "查无设备信息",
    }


def _slim_firmware_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "country": item.get("country"),
        "product": item.get("product"),
        "firmware_version": item.get("firmware_version"),
        "firmware_id": item.get("firmware_id"),
        "allowed_push": item.get("allowed_push"),
        "is_for_outside": item.get("is_for_outside"),
        "remark": item.get("remark"),
        "remark_for_outside": item.get("remark_for_outside"),
        "need_monitor": item.get("need_monitor"),
        "created_at": item.get("created_at"),
    }


def _pick_latest_firmware(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not items:
        return None
    return max(items, key=lambda item: item.get("firmware_version") or "")


async def tool_get_firmware_infos(
    product: str = "",
    country: str = "cn",
    firmware_version: str = "",
    firmware_id: str = "",
    **_: Any,
) -> dict[str, Any]:
    model = (product or "").strip()
    if not model:
        return {"tool": "get_firmware_infos", "error": "请提供产品型号"}

    region = (country or "cn").strip().lower() or "cn"

    try:
        response = await get_firmware_infos(
            country=region,
            product=model,
            firmware_version=(firmware_version or "").strip() or None,
            firmware_id=(firmware_id or "").strip() or None,
            per_page=500,
            page=1,
        )
    except Exception as exc:
        return {
            "tool": "get_firmware_infos",
            "country": region,
            "product": model,
            "error": str(exc),
        }

    if not response or response.code != CodeEnum.SUCCESS:
        return {
            "tool": "get_firmware_infos",
            "country": region,
            "product": model,
            "error": getattr(response, "message", None) or "查无固件版本信息",
        }

    payload = response.data if isinstance(response.data, dict) else {}
    raw_items = payload.get("items") if isinstance(payload.get("items"), list) else []
    items = [_slim_firmware_item(item) for item in raw_items if isinstance(item, dict)]

    pushable_items = [item for item in items if item.get("allowed_push")]
    market_items = [
        item for item in items
        if item.get("allowed_push") and item.get("is_for_outside")
    ]

    latest_pushable = _pick_latest_firmware(pushable_items)
    latest_market = _pick_latest_firmware(market_items)

    return {
        "tool": "get_firmware_infos",
        "country": region,
        "product": model,
        "total": payload.get("total", len(items)),
        "latest_pushable_market_version": latest_market,
        "latest_pushable_version": latest_pushable,
        "pushable_count": len(pushable_items),
        "market_count": len(market_items),
        "items": items,
    }


async def tool_push_firmware(
    did_or_sn: str = "",
    firmware_id: str = "",
    **_: Any,
) -> dict[str, Any]:
    key = (did_or_sn or "").strip()
    fir_id = (firmware_id or "").strip()
    if not key:
        return {"tool": "push_firmware", "error": "请提供设备 SN 或 DID"}
    if not fir_id:
        return {"tool": "push_firmware", "did_or_sn": key, "error": "请提供 firmware_id"}

    try:
        response = await push_firmware_api(did_or_sn=key, firmware_id=fir_id)
    except Exception as exc:
        return {
            "tool": "push_firmware",
            "did_or_sn": key,
            "firmware_id": fir_id,
            "error": str(exc),
        }

    if response and response.code == CodeEnum.SUCCESS:
        return {
            "tool": "push_firmware",
            "did_or_sn": key,
            "firmware_id": fir_id,
            "message": getattr(response, "message", None) or "推送固件成功",
        }

    return {
        "tool": "push_firmware",
        "did_or_sn": key,
        "firmware_id": fir_id,
        "error": getattr(response, "message", None) or "固件推送失败",
    }


async def tool_get_ota_router(did_or_sn: str = "", **_: Any) -> dict[str, Any]:
    key = (did_or_sn or "").strip()
    if not key:
        return {"tool": "get_ota_router", "error": "请提供设备 SN 或 DID"}

    try:
        response = await robot_ota_router(did_or_sn=key)
    except Exception as exc:
        return {"tool": "get_ota_router", "did_or_sn": key, "error": str(exc)}

    if response and response.code == CodeEnum.SUCCESS:
        data = response.data or {}
        router_list = data.get("router") if isinstance(data, dict) else data
        return {
            "tool": "get_ota_router",
            "did_or_sn": key,
            "data": {
                "router": router_list,
                "route_count": len(router_list) if isinstance(router_list, list) else 0,
            },
        }

    return {
        "tool": "get_ota_router",
        "did_or_sn": key,
        "error": getattr(response, "message", None) or "查无 OTA 升级路线",
    }


async def tool_get_bag_upload_status(did_or_sn: str = "", **_: Any) -> dict[str, Any]:
    key = (did_or_sn or "").strip()
    if not key:
        return {"tool": "get_bag_upload_status", "error": "请提供设备 SN 或 DID"}

    try:
        response = await get_bag_upload_status_api(did_or_sn=key)
    except Exception as exc:
        return {"tool": "get_bag_upload_status", "did_or_sn": key, "error": str(exc)}

    if response and response.code == CodeEnum.SUCCESS:
        data = dict(response.data or {})
        upload_on = bool(data.get("upload_status"))
        data["upload_status_text"] = "已开启立即上传" if upload_on else "未开启立即上传"
        return {
            "tool": "get_bag_upload_status",
            "did_or_sn": key,
            "message": getattr(response, "message", None),
            "data": data,
        }

    return {
        "tool": "get_bag_upload_status",
        "did_or_sn": key,
        "error": getattr(response, "message", None) or "查无 Bag 上传设置信息",
    }


async def tool_switch_bag_upload(
    did_or_sn: str = "",
    upload_status: Any = None,
    **_: Any,
) -> dict[str, Any]:
    key = (did_or_sn or "").strip()
    if not key:
        return {"tool": "switch_bag_upload", "error": "请提供设备 SN 或 DID"}

    status = _parse_bool(upload_status)
    if status is None:
        return {
            "tool": "switch_bag_upload",
            "did_or_sn": key,
            "error": "请明确 upload_status：true 打开，false 关闭",
        }

    try:
        response = await switch_bag_upload_api(did_or_sn=key, upload_status=status)
    except Exception as exc:
        return {"tool": "switch_bag_upload", "did_or_sn": key, "error": str(exc)}

    if response and response.code == CodeEnum.SUCCESS:
        data = dict(response.data or {})
        data["upload_status"] = status
        data["upload_status_text"] = "已开启立即上传" if status else "未开启立即上传"
        return {
            "tool": "switch_bag_upload",
            "did_or_sn": key,
            "message": getattr(response, "message", None),
            "data": data,
        }

    return {
        "tool": "switch_bag_upload",
        "did_or_sn": key,
        "upload_status": status,
        "error": getattr(response, "message", None) or "Bag 开关操作失败",
    }


def _parse_appid_lines(appid_text: str) -> tuple[list[list[Any]], str | None]:
    appids: list[list[Any]] = []
    for line in appid_text.split("\n"):
        if not line.strip():
            continue
        parts = line.strip().split()
        if parts[0].isdigit():
            appids.append([int(parts[0]), parts[-1]])
        else:
            return [], f"APPID 格式错误: {line.strip()}，要求每行一个数字 APPID"
    if not appids:
        return [], "请提供至少一个 APPID"
    return appids, None


async def tool_add_users_to_app(
    appid: str = "",
    context: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    text = (appid or "").strip()
    if not text:
        return {"tool": "add_users_to_app", "error": "请提供 APPID"}

    context = context or {}
    creator = (
        str(context.get("username") or "").strip()
        or str(context.get("user_email") or "").strip()
    )
    if not creator:
        return {"tool": "add_users_to_app", "error": "无法识别操作人，请重新登录后再试"}

    appids, parse_error = _parse_appid_lines(text)
    if parse_error:
        return {"tool": "add_users_to_app", "error": parse_error}

    door_guard = DoorGuardAPI()
    errors: list[list[Any]] = []
    success_items: list[dict[str, Any]] = []

    for aid in appids:
        app_id = aid[0]
        name = str(aid[-1])
        try:
            result = await door_guard.app_add_users(appid=app_id, name=name)
        except Exception as exc:
            errors.append([app_id, name])
            continue

        if result.get("success", False):
            await AppAddWhiteRecordModel.create(appid=app_id, name=name, creator=creator)
            success_items.append({"appid": app_id, "name": name})
        else:
            errors.append([app_id, name])

    if errors:
        return {
            "tool": "add_users_to_app",
            "creator": creator,
            "success_count": len(success_items),
            "success_items": success_items,
            "failed_items": [{"appid": item[0], "name": str(item[-1])} for item in errors],
            "error": f"部分 APPID 添加失败: {errors}",
        }

    return {
        "tool": "add_users_to_app",
        "creator": creator,
        "success_count": len(success_items),
        "success_items": success_items,
        "message": "所有 APPID 已添加成功",
    }


async def tool_device_network_diagnosis(
    did_or_sn: str = "",
    start_time: str = "",
    end_time: str = "",
    **_: Any,
) -> dict[str, Any]:
    key = (did_or_sn or "").strip()
    if not key:
        return {"tool": "device_network_diagnosis", "error": "请提供设备 SN 或 DID"}

    now = datetime.now()
    st_dt = _parse_diagnosis_datetime(
        start_time,
        (now - timedelta(days=DEFAULT_NETWORK_DIAGNOSIS_DAYS)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ),
    )
    ed_text = (end_time or "").strip()
    ed_default = now
    ed_end_of_day = bool(ed_text and " " not in ed_text and len(ed_text) == 10)
    ed_dt = _parse_diagnosis_datetime(end_time, ed_default, end_of_day=ed_end_of_day)

    try:
        response = await device_network_diagnosis(
            did_or_sn=key,
            st_time=st_dt,
            ed_time=ed_dt,
        )
    except Exception as exc:
        return {
            "tool": "device_network_diagnosis",
            "did_or_sn": key,
            "error": str(exc),
        }

    if response and response.code == CodeEnum.SUCCESS:
        records = response.data if isinstance(response.data, list) else []
        return {
            "tool": "device_network_diagnosis",
            "did_or_sn": key,
            "start_time": st_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": ed_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(records),
            "records": records,
        }

    return {
        "tool": "device_network_diagnosis",
        "did_or_sn": key,
        "start_time": st_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": ed_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "error": getattr(response, "message", None) or "查无网络诊断数据",
    }


async def tool_get_nw_config_history(key: str = "", **_: Any) -> dict[str, Any]:
    query_key = (key or "").strip()
    if not query_key:
        return {"tool": "get_nw_config_history", "error": "请提供 SN、DID、uid 或 uuid"}

    try:
        response = await get_nw_config_historys(key=query_key)
    except Exception as exc:
        return {"tool": "get_nw_config_history", "key": query_key, "error": str(exc)}

    if response and response.code == CodeEnum.SUCCESS:
        data = response.data if isinstance(response.data, dict) else {}
        items = data.get("items") if isinstance(data, dict) else []
        total = data.get("total") if isinstance(data, dict) else len(items or [])
        return {
            "tool": "get_nw_config_history",
            "key": query_key,
            "total": total,
            "items": items or [],
        }

    return {
        "tool": "get_nw_config_history",
        "key": query_key,
        "error": getattr(response, "message", None) or "查无配网记录",
    }


async def tool_get_sn_info(sn_number: str = "", **_: Any) -> dict[str, Any]:
    key = (sn_number or "").strip()
    if not key:
        return {"tool": "get_sn_info", "error": "请提供设备 SN"}

    try:
        sn_response = await get_sn_info_api(sn_number=key)
    except Exception as exc:
        return {"tool": "get_sn_info", "sn_number": key, "error": str(exc)}

    if sn_response and sn_response.code == CodeEnum.SUCCESS:
        return {
            "tool": "get_sn_info",
            "sn_number": key,
            "data": sn_response.data or {},
        }

    return {
        "tool": "get_sn_info",
        "sn_number": key,
        "error": getattr(sn_response, "message", None) or "查无 SN 关联信息",
    }


async def _tool_sn_info_from_api(
    tool_name: str,
    sn_number: str,
    api_func: Any,
) -> dict[str, Any]:
    key = (sn_number or "").strip()
    if not key:
        return {"tool": tool_name, "error": "请提供设备 SN"}

    try:
        sn_response = await api_func(sn_number=key)
    except Exception as exc:
        return {"tool": tool_name, "sn_number": key, "error": str(exc)}

    if sn_response and sn_response.code == CodeEnum.SUCCESS:
        return {
            "tool": tool_name,
            "sn_number": key,
            "data": sn_response.data or {},
        }

    return {
        "tool": tool_name,
        "sn_number": key,
        "error": getattr(sn_response, "message", None) or "查无 SN 关联信息",
    }


async def tool_get_sn_info_j1(sn_number: str = "", **_: Any) -> dict[str, Any]:
    return await _tool_sn_info_from_api("get_sn_info_j1", sn_number, get_sn_info_api_j1)


async def tool_get_sn_info_j2(sn_number: str = "", **_: Any) -> dict[str, Any]:
    return await _tool_sn_info_from_api("get_sn_info_j2", sn_number, get_sn_info_api_j2)


async def tool_get_dlog_data(
    did_or_sn: str = "",
    start_date: str = "",
    end_date: str = "",
    **_: Any,
) -> dict[str, Any]:
    key = (did_or_sn or "").strip()
    if not key:
        return {"tool": "get_dlog_data", "error": "请提供设备 DID 或 SN"}

    today = date.today()
    st_date = _parse_dlog_date(start_date, today - timedelta(days=DEFAULT_DLOG_LOOKBACK_DAYS))
    ed_date = _parse_dlog_date(end_date, today)
    log_path = ""

    try:
        download_result = await download_logs(key, st_date, ed_date)
        log_path = download_result.get("log_path") or ""
        result_code = download_result.get("result")

        if result_code == DlogResult.Success_Log:
            raw = await get_dlog_datas(log_path, key, DEFAULT_DLOG_COMMANDS)
            dlog_data = _normalize_dlog_result(raw)
            return {
                "tool": "get_dlog_data",
                "did_or_sn": key,
                "start_date": st_date,
                "end_date": ed_date,
                "data": {"dlog_data": dlog_data},
            }

        if result_code == DlogResult.Success_No_Log:
            return {
                "tool": "get_dlog_data",
                "did_or_sn": key,
                "start_date": st_date,
                "end_date": ed_date,
                "error": f"该时间段（{st_date} ~ {ed_date}）内无设备日志",
            }

        return {
            "tool": "get_dlog_data",
            "did_or_sn": key,
            "start_date": st_date,
            "end_date": ed_date,
            "error": "设备日志下载失败，请确认 DID/SN 是否正确或稍后重试",
        }
    except Exception as exc:
        return {
            "tool": "get_dlog_data",
            "did_or_sn": key,
            "start_date": st_date,
            "end_date": ed_date,
            "error": str(exc),
        }
    finally:
        if log_path:
            get_logs_dir(log_path, key, clear=True)


async def tool_get_voice_code_info(code: str = "", **_: Any) -> dict[str, Any]:
    from .voice_code_info import lookup_voice_code_info

    result = await lookup_voice_code_info(code)
    return {"tool": "get_voice_code_info", **result}


async def tool_get_error_code_info(code: str = "", **_: Any) -> dict[str, Any]:
    from .error_code_info import lookup_error_code_info

    result = await lookup_error_code_info(code)
    return {"tool": "get_error_code_info", **result}


async def tool_get_app_logs(
    user_account: str = "",
    region: str = "",
    start_time: str = "",
    end_time: str = "",
    **_: Any,
) -> dict[str, Any]:
    from .app_logs import fetch_app_logs

    result = await fetch_app_logs(
        user_account,
        region=region,
        start_time=start_time,
        end_time=end_time,
    )
    return {"tool": "get_app_logs", **result}


async def tool_get_current_time(**_: Any) -> dict[str, Any]:
    now = datetime.now(pytz.timezone("Asia/Shanghai"))
    weekday_labels = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    return {
        "tool": "get_current_time",
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "timezone": "Asia/Shanghai",
        "utc_offset": "+08:00",
        "weekday": weekday_labels[now.weekday()],
        "iso8601": now.isoformat(),
    }


TOOL_REGISTRY: dict[str, ToolHandler] = {
    "search_knowledge": tool_search_knowledge,
    "list_product_catalog": tool_list_product_catalog,
    "list_attr_fields": tool_list_attr_fields,
    "get_product_attrs": tool_get_product_attrs,
    "scan_product_attrs": tool_scan_product_attrs,
    "get_robot_info": tool_get_robot_info,
    "get_ota_router": tool_get_ota_router,
    "get_firmware_infos": tool_get_firmware_infos,
    "push_firmware": tool_push_firmware,
    "get_bag_upload_status": tool_get_bag_upload_status,
    "switch_bag_upload": tool_switch_bag_upload,
    "add_users_to_app": tool_add_users_to_app,
    "device_network_diagnosis": tool_device_network_diagnosis,
    "get_nw_config_history": tool_get_nw_config_history,
    "get_sn_info": tool_get_sn_info,
    "get_sn_info_j1": tool_get_sn_info_j1,
    "get_sn_info_j2": tool_get_sn_info_j2,
    "get_dlog_data": tool_get_dlog_data,
    "get_app_logs": tool_get_app_logs,
    "get_error_code_info": tool_get_error_code_info,
    "get_voice_code_info": tool_get_voice_code_info,
    "get_current_time": tool_get_current_time,
}


async def execute_tool(
    tool_name: str,
    action_input: dict[str, Any] | None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action_input = action_input or {}
    context = context or {}

    handler = TOOL_REGISTRY.get(tool_name)
    if not handler:
        return {"tool": tool_name, "error": f"未知工具: {tool_name}"}

    if tool_name == "search_knowledge":
        return await handler(
            query=str(action_input.get("query") or ""),
            context=context,
        )
    if tool_name == "list_product_catalog":
        return await handler(
            region=str(action_input.get("region") or ""),
            context=context,
        )
    if tool_name == "list_attr_fields":
        return await handler(
            region=str(action_input.get("region") or ""),
            context=context,
        )
    if tool_name == "get_product_attrs":
        return await handler(
            product_names=action_input.get("product_names"),
            region=str(action_input.get("region") or ""),
            field_keys=action_input.get("field_keys"),
            context=context,
        )
    if tool_name == "scan_product_attrs":
        return await handler(
            field_keys=action_input.get("field_keys"),
            region=str(action_input.get("region") or ""),
            context=context,
        )
    if tool_name == "get_robot_info":
        return await handler(did_or_sn=str(action_input.get("did_or_sn") or ""))
    if tool_name == "get_ota_router":
        return await handler(did_or_sn=str(action_input.get("did_or_sn") or ""))
    if tool_name == "get_firmware_infos":
        return await handler(
            product=str(action_input.get("product") or ""),
            country=str(action_input.get("country") or "cn"),
            firmware_version=str(action_input.get("firmware_version") or ""),
            firmware_id=str(action_input.get("firmware_id") or ""),
        )
    if tool_name == "push_firmware":
        return await handler(
            did_or_sn=str(action_input.get("did_or_sn") or ""),
            firmware_id=str(action_input.get("firmware_id") or ""),
        )
    if tool_name == "get_bag_upload_status":
        return await handler(did_or_sn=str(action_input.get("did_or_sn") or ""))
    if tool_name == "switch_bag_upload":
        return await handler(
            did_or_sn=str(action_input.get("did_or_sn") or ""),
            upload_status=action_input.get("upload_status"),
        )
    if tool_name == "add_users_to_app":
        return await handler(
            appid=str(action_input.get("appid") or ""),
            context=context,
        )
    if tool_name == "device_network_diagnosis":
        return await handler(
            did_or_sn=str(action_input.get("did_or_sn") or ""),
            start_time=str(action_input.get("start_time") or ""),
            end_time=str(action_input.get("end_time") or ""),
        )
    if tool_name == "get_nw_config_history":
        return await handler(key=str(action_input.get("key") or action_input.get("did_or_sn") or ""))
    if tool_name == "get_sn_info":
        return await handler(sn_number=str(action_input.get("sn_number") or ""))
    if tool_name == "get_sn_info_j1":
        return await handler(sn_number=str(action_input.get("sn_number") or ""))
    if tool_name == "get_sn_info_j2":
        return await handler(sn_number=str(action_input.get("sn_number") or ""))
    if tool_name == "get_dlog_data":
        return await handler(
            did_or_sn=str(action_input.get("did_or_sn") or ""),
            start_date=str(action_input.get("start_date") or ""),
            end_date=str(action_input.get("end_date") or ""),
        )
    if tool_name == "get_app_logs":
        return await handler(
            user_account=str(action_input.get("user_account") or ""),
            region=str(action_input.get("region") or ""),
            start_time=str(action_input.get("start_time") or ""),
            end_time=str(action_input.get("end_time") or ""),
        )
    if tool_name == "get_voice_code_info":
        return await handler(code=str(action_input.get("code") or ""))
    if tool_name == "get_error_code_info":
        return await handler(code=str(action_input.get("code") or ""))
    if tool_name == "get_current_time":
        return await handler()
    return {"tool": tool_name, "error": "工具参数不匹配"}
