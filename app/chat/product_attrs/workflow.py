# -*- coding: utf-8 -*-
#
# 查询产品参数 workflow（先 LLM 选型号，再查 MySQL）
#
from typing import AsyncIterator

from chat.common.workflow import emit_workflow_finish, WorkflowRunResult
from chat.intent import IntentResult
from chat.product_attrs.query import query_product_attrs_for_question
from chat.product_attrs.service import normalize_region
from chat.react import ReactEvent, ReactStepRecord
from chat.schemas import AgentChatRequest

PRODUCT_ATTRS_SYSTEM = (
    "你是云鲸（Narwal）扫地/扫拖机器人产品参数专家。"
    "根据提供的相关产品参数 JSON 回答用户问题。"
    "规则：\n"
    "1. 只依据参数数据回答，不要编造\n"
    "2. 可进行型号对比，条理清晰\n"
    "3. 数据中没有的信息如实说明\n"
    "4. 使用中文，面向 FAE/客服\n"
    "5. 参数 JSON 的键为 product_name（规范型号名），用户可能使用别名/简称提问\n"
    "6. 若用户提到的名称出现在「已选产品型号及别名」的别名列表中，"
    "表示已查到该产品，请直接用对应规范型号的参数回答，不要说找不到该别名"
)

PRODUCT_ATTRS_SCAN_SYSTEM = (
    "你是云鲸（Narwal）扫地/扫拖机器人产品参数专家。"
    "用户未指定具体型号，需要在全产品库中筛选、对比或排序。"
    "规则：\n"
    "1. 只依据提供的各产品参数字段数据回答，不要编造\n"
    "2. 每个产品下 attrs 的键为 field_key（英文），对照「查询字段说明」理解含义\n"
    "3. 参数值为文本（如 31000Pa），比较数值时需自行解析单位\n"
    "4. 筛选类问题（如吸力大于某值）请遍历全部产品后列出符合条件的型号\n"
    "5. 排序类问题请比较后给出排名\n"
    "6. 使用中文，面向 FAE/客服"
)


async def run_product_attrs_workflow(
    payload: AgentChatRequest,
    intent_result: IntentResult,
) -> AsyncIterator[ReactEvent]:
    steps: list[ReactStepRecord] = []
    step_idx = 1
    context = dict(payload.context or {})
    region = normalize_region(context.get("region"))
    user_question = payload.get_current_user_content()

    yield ReactEvent(
        type="tool_start",
        step=step_idx,
        data={"tool_name": "select_product_models", "action_input": {"region": region}},
    )

    attrs_result = await query_product_attrs_for_question(
        user_question,
        context,
        history_messages=payload.get_history_messages(),
    )
    observation = {
        "tool": "query_product_attrs",
        "mode": attrs_result.get("mode"),
        "region": attrs_result.get("region"),
        "products": attrs_result.get("products"),
        "field_keys": attrs_result.get("field_keys"),
        "select": attrs_result.get("select"),
        "field_select": attrs_result.get("field_select"),
        "missing_products": attrs_result.get("missing_products"),
        "catalog_count": attrs_result.get("catalog_count"),
        "has_data": bool(attrs_result.get("attrs")),
        "error": attrs_result.get("error"),
    }

    steps.append(
        ReactStepRecord(
            step=step_idx,
            thought="",
            action="select_and_query_product_attrs",
            action_input={"region": region, "question": user_question},
            observation=observation,
        )
    )
    yield ReactEvent(
        type="tool_done",
        step=step_idx,
        data={"tool_name": "select_and_query_product_attrs", "result": observation},
    )

    if attrs_result.get("error") or not attrs_result.get("attrs"):
        result = WorkflowRunResult(
            mode="workflow_query_product_attrs",
            intent="query_product_attrs",
            steps=steps,
            final_answer=attrs_result.get("error") or "未查询到产品参数，请指明产品型号。",
            workflow_data=observation,
        )
        async for event in emit_workflow_finish(payload, result):
            yield event
        return

    attrs_json = attrs_result.get("content") or ""
    products = attrs_result.get("products") or []
    mode = attrs_result.get("mode") or "specific"

    if mode == "scan":
        field_desc = attrs_result.get("field_descriptions") or ""
        user_prompt = (
            f"区域（region）：{region or '未指定'}\n"
            f"用户当前问题：{user_question}\n"
            f"查询模式：全库扫描（共 {len(products)} 款产品）\n"
            f"查询字段说明：\n{field_desc}\n\n"
            "请根据各产品的字段值回答筛选/对比/排序类问题。\n\n"
            f"全库产品参数数据（JSON，外层键为 product_name，内层键为 field_key）：\n{attrs_json}"
        )
        system_prompt = PRODUCT_ATTRS_SCAN_SYSTEM
    else:
        alias_mapping = attrs_result.get("alias_mapping") or ""
        user_prompt = (
            f"区域（region）：{region or '未指定'}\n"
            f"用户当前问题：{user_question}\n"
            f"已选产品型号及别名：\n{alias_mapping}\n\n"
            "说明：用户使用的简称/别名若出现在上述别名列表中，即对应右侧规范型号，"
            "请直接用该型号在 JSON 中的参数作答，勿称找不到该别名。\n\n"
            f"产品参数数据（JSON，键为 product_name）：\n{attrs_json}"
        )
        system_prompt = PRODUCT_ATTRS_SYSTEM

    result = WorkflowRunResult(
        mode="workflow_query_product_attrs",
        intent="query_product_attrs",
        steps=steps,
        workflow_data={
            "region": region,
            "mode": mode,
            "products": products,
            "field_keys": attrs_result.get("field_keys"),
            "select": attrs_result.get("select"),
            "field_select": attrs_result.get("field_select"),
            "missing_products": attrs_result.get("missing_products"),
        },
    )

    async for event in emit_workflow_finish(
        payload,
        result,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    ):
        yield event
