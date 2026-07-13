# -*- coding: utf-8 -*-
#
# 推理模型 + 原生 tool calling 多轮工具调用
#
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal

from common.logger import logger

from chat.generation_cancel import generation_id_from_context, is_generation_cancelled
from chat.llm import DEEPSEEK_MODEL, complete_with_tools, iter_completion_deltas
from chat.locale import (
    format_thinking_max_steps,
    get_locale_from_context,
)
from chat.prompts import build_agent_system_prompt, build_final_answer_system_prompt
from chat.schemas import AgentChatRequest
from chat.skill_router import (
    SkillSelectionResult,
    build_skill_selection_event_data,
    select_skills,
)
from chat.skills.loader import (
    all_agent_skill_ids,
    should_route_create_jira_workflow,
)
from chat.tool_registry import ToolRegistry
from chat.tools import build_openai_tools, execute_tool
from chat.action_confirm import CONFIRM_REQUIRED_TOOLS, build_action_required_payload

DEFAULT_MAX_STEPS = 6

TOOL_ACTIONS = ToolRegistry.names()


@dataclass
class ReactStepRecord:
    step: int
    thought: str
    action: str
    action_input: dict[str, Any]
    observation: dict[str, Any] | None = None


@dataclass
class ReactRunResult:
    steps: list[ReactStepRecord] = field(default_factory=list)
    final_answer: str = ""


ReactEventType = Literal[
    "skill_routing",
    "skills_selected",
    "thought",
    "tool_start",
    "tool_done",
    "finish",
    "max_steps",
    "action_required",
]


@dataclass
class ReactEvent:
    type: ReactEventType
    step: int
    data: dict[str, Any]


def _build_history_messages(payload: AgentChatRequest) -> list[dict[str, str]]:
    return payload.get_history_messages()


def _build_initial_messages(
    payload: AgentChatRequest,
    skill_ids: list[str],
) -> list[dict[str, Any]]:
    locale = get_locale_from_context(payload.context)
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": build_agent_system_prompt(locale, skill_ids=skill_ids),
        },
    ]
    messages.extend(_build_history_messages(payload))
    messages.append({"role": "user", "content": payload.get_current_user_content()})
    return messages


def _assistant_message_dict(message: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
    if message.tool_calls:
        data["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments or "{}",
                },
            }
            for tc in message.tool_calls
        ]
    return data


def _parse_tool_arguments(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _get_reasoning_content(message: Any) -> str:
    reasoning = getattr(message, "reasoning_content", None)
    return (reasoning or "").strip()


async def stream_plain_text(text: str) -> AsyncIterator[str]:
    for char in text or "":
        yield char


CANCELLED_TOOL_OBSERVATION: dict[str, Any] = {
    "cancelled": True,
    "message": "用户已取消该工具执行",
}


def _tool_call_item_dict(tool_call: Any) -> dict[str, str]:
    return {
        "id": tool_call.id,
        "name": tool_call.function.name,
        "arguments": tool_call.function.arguments or "{}",
    }


def _tool_call_items_from_message(tool_calls: Any, start_index: int = 0) -> list[dict[str, str]]:
    return [_tool_call_item_dict(tc) for tc in tool_calls[start_index:]]


def append_tool_message(
    messages: list[dict[str, Any]],
    tool_call_id: str,
    observation: Any,
) -> None:
    messages.append({
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(observation, ensure_ascii=False),
    })


def append_cancelled_tool_messages(
    messages: list[dict[str, Any]],
    tool_call_items: list[dict[str, Any]],
) -> None:
    for item in tool_call_items:
        tool_call_id = item.get("id") or ""
        if not tool_call_id:
            continue
        append_tool_message(messages, tool_call_id, CANCELLED_TOOL_OBSERVATION)


async def _run_tool_with_context(
    payload: AgentChatRequest,
    context: dict[str, Any],
    tool_name: str,
    action_input: dict[str, Any],
) -> dict[str, Any]:
    generation_id = generation_id_from_context(payload.context)
    if generation_id and await is_generation_cancelled(generation_id):
        return CANCELLED_TOOL_OBSERVATION

    tool_context = dict(context)
    tool_context.setdefault("user_question", payload.get_current_user_content())
    tool_context.setdefault("history_messages", payload.get_history_messages())
    try:
        return await execute_tool(tool_name, action_input, context=tool_context)
    except Exception as exc:
        logger.error(f"工具执行失败 tool={tool_name}", exc_info=True)
        return {"tool": tool_name, "error": str(exc)}


def _build_action_required_for_tool(
    tool_call_item: dict[str, str],
    action_input: dict[str, Any],
    messages: list[dict[str, Any]],
    steps: list[ReactStepRecord],
    remaining_tool_calls: list[dict[str, str]],
    step_idx: int,
    *,
    selected_skill_ids: list[str] | None = None,
    direct_reply: bool = False,
) -> dict[str, Any]:
    tool_name = tool_call_item.get("name") or ""
    action_id = str(uuid.uuid4())
    preview_text = (
        f"即将执行工具「{tool_name}」，参数如下：\n"
        f"{json.dumps(action_input, ensure_ascii=False, indent=2)}\n\n"
        "请确认后执行，或取消操作。"
    )
    action_payload = build_action_required_payload(
        action_type=tool_name,
        draft=action_input,
        preview_text=preview_text,
        kind="react",
        mode="react",
        steps=[_step_to_dict(s) for s in steps],
        action_id=action_id,
    )
    return {
        **action_payload,
        "react_messages": messages,
        "pending_tool_call": tool_call_item,
        "remaining_tool_calls": remaining_tool_calls,
        "step_idx": step_idx,
        "selected_skill_ids": selected_skill_ids or [],
        "direct_reply": direct_reply,
    }


async def process_remaining_tool_calls_after_confirm(
    payload: AgentChatRequest,
    messages: list[dict[str, Any]],
    steps: list[ReactStepRecord],
    remaining_tool_calls: list[dict[str, Any]],
    start_index: int,
    step_idx: int,
    *,
    selected_skill_ids: list[str] | None = None,
    direct_reply: bool = False,
) -> dict[str, Any] | None:
    """处理同轮 assistant 内剩余 tool_calls；再次遇到需确认工具时返回 action_required data。"""
    context = payload.context or {}
    for idx in range(start_index, len(remaining_tool_calls)):
        item = remaining_tool_calls[idx]
        tool_name = item.get("name") or ""
        action_input = _parse_tool_arguments(item.get("arguments"))
        if tool_name in CONFIRM_REQUIRED_TOOLS:
            remaining = remaining_tool_calls[idx:]
            return _build_action_required_for_tool(
                remaining[0],
                action_input,
                messages,
                steps,
                remaining,
                step_idx,
                selected_skill_ids=selected_skill_ids,
                direct_reply=direct_reply,
            )
        observation = await _run_tool_with_context(
            payload, context, tool_name, action_input,
        )
        steps.append(
            ReactStepRecord(
                step=step_idx,
                thought="",
                action=tool_name,
                action_input=action_input,
                observation=observation,
            )
        )
        append_tool_message(messages, item.get("id") or "", observation)
    return None


async def process_after_pending_tool_cancelled(
    payload: AgentChatRequest,
    messages: list[dict[str, Any]],
    steps: list[ReactStepRecord],
    remaining_tool_calls: list[dict[str, Any]],
    pending_tool_call: dict[str, Any],
    step_idx: int,
    *,
    selected_skill_ids: list[str] | None = None,
    direct_reply: bool = False,
) -> dict[str, Any] | None:
    """当前 pending 工具已取消/超时后，仅写入该 tool 消息，再处理同轮剩余 tool_calls。"""
    current_item = pending_tool_call or (remaining_tool_calls[0] if remaining_tool_calls else {})
    tool_call_id = current_item.get("id") or ""
    if tool_call_id:
        append_cancelled_tool_messages(messages, [current_item])
        tool_name = current_item.get("name") or ""
        action_input = _parse_tool_arguments(current_item.get("arguments"))
        steps.append(
            ReactStepRecord(
                step=step_idx,
                thought="",
                action=tool_name,
                action_input=action_input,
                observation=CANCELLED_TOOL_OBSERVATION,
            )
        )

    if not remaining_tool_calls:
        return None

    return await process_remaining_tool_calls_after_confirm(
        payload,
        messages,
        steps,
        remaining_tool_calls,
        1,
        step_idx,
        selected_skill_ids=selected_skill_ids,
        direct_reply=direct_reply,
    )


async def _run_react_steps(
    payload: AgentChatRequest,
    messages: list[dict[str, Any]],
    steps: list[ReactStepRecord],
    openai_tools: list[dict[str, Any]],
    *,
    start_step: int,
    max_steps: int,
    selected_skill_ids: list[str],
) -> AsyncIterator[ReactEvent]:
    context = payload.context or {}
    llm_temperature = payload.temperature if payload.temperature is not None else 0.2

    for step_idx in range(start_step, max_steps + 1):
        generation_id = generation_id_from_context(payload.context)
        if generation_id and await is_generation_cancelled(generation_id):
            return

        try:
            response = await complete_with_tools(
                messages=messages,
                tools=openai_tools,
                temperature=llm_temperature,
            )
        except Exception as exc:
            logger.error(f"推理模型调用失败 step={step_idx}", exc_info=True)
            raise

        choice = response.choices[0]
        message = choice.message
        reasoning = _get_reasoning_content(message)

        if reasoning:
            yield ReactEvent(
                type="thought",
                step=step_idx,
                data={"reasoning_content": reasoning},
            )

        if message.tool_calls:
            messages.append(_assistant_message_dict(message))
            for idx, tool_call in enumerate(message.tool_calls):
                tool_name = tool_call.function.name
                action_input = _parse_tool_arguments(tool_call.function.arguments)

                yield ReactEvent(
                    type="tool_start",
                    step=step_idx,
                    data={"tool_name": tool_name, "action_input": action_input},
                )

                if tool_name in CONFIRM_REQUIRED_TOOLS:
                    remaining_tool_calls = _tool_call_items_from_message(message.tool_calls, idx)
                    action_data = _build_action_required_for_tool(
                        remaining_tool_calls[0],
                        action_input,
                        messages,
                        steps,
                        remaining_tool_calls,
                        step_idx,
                        selected_skill_ids=selected_skill_ids,
                    )
                    yield ReactEvent(
                        type="action_required",
                        step=step_idx,
                        data=action_data,
                    )
                    return

                if generation_id and await is_generation_cancelled(generation_id):
                    return

                observation = await _run_tool_with_context(
                    payload, context, tool_name, action_input,
                )

                yield ReactEvent(
                    type="tool_done",
                    step=step_idx,
                    data={"tool_name": tool_name, "result": observation},
                )

                steps.append(
                    ReactStepRecord(
                        step=step_idx,
                        thought=reasoning,
                        action=tool_name,
                        action_input=action_input,
                        observation=observation,
                    )
                )

                append_tool_message(messages, tool_call.id, observation)
            continue

        final_answer = (message.content or "").strip()
        if not final_answer and reasoning:
            final_answer = reasoning

        yield ReactEvent(
            type="finish",
            step=step_idx,
            data={
                "final_answer": final_answer,
                "steps_count": len(steps),
                "steps": [_step_to_dict(s) for s in steps],
                "skill_ids": selected_skill_ids,
                "mode": "react",
                "intent": None,
            },
        )
        return

    yield ReactEvent(
        type="max_steps",
        step=max_steps,
        data={
            "message": format_thinking_max_steps(
                max_steps,
                get_locale_from_context(payload.context),
            ),
            "steps": [_step_to_dict(s) for s in steps],
            "skill_ids": selected_skill_ids,
        },
    )


async def run_react_loop(payload: AgentChatRequest) -> AsyncIterator[ReactEvent]:
    max_steps = payload.max_steps or DEFAULT_MAX_STEPS
    steps: list[ReactStepRecord] = []

    yield ReactEvent(type="skill_routing", step=0, data={})
    selection: SkillSelectionResult = await select_skills(payload)
    yield ReactEvent(
        type="skills_selected",
        step=0,
        data=build_skill_selection_event_data(selection),
    )

    if should_route_create_jira_workflow(selection.skill_ids):
        from chat.intent import IntentResult
        from chat.jira.workflow import run_create_jira_workflow

        intent_result = IntentResult(
            intent="create_jira",
            confidence=selection.confidence,
            source=f"skill:{selection.source}",
        )
        async for event in run_create_jira_workflow(payload, intent_result):
            yield event
        return

    skill_ids = selection.skill_ids
    openai_tools = build_openai_tools(skill_ids)
    messages = _build_initial_messages(payload, skill_ids)

    async for event in _run_react_steps(
        payload,
        messages,
        steps,
        openai_tools,
        start_step=1,
        max_steps=max_steps,
        selected_skill_ids=skill_ids,
    ):
        yield event


async def continue_react_loop(
    payload: AgentChatRequest,
    messages: list[dict[str, Any]],
    steps: list[ReactStepRecord],
    start_step: int,
    *,
    skill_ids: list[str] | None = None,
) -> AsyncIterator[ReactEvent]:
    """从检查点恢复 ReAct 循环（已写入 tool observation 之后）。"""
    max_steps = payload.max_steps or DEFAULT_MAX_STEPS
    selected = skill_ids or all_agent_skill_ids()
    openai_tools = build_openai_tools(selected)

    async for event in _run_react_steps(
        payload,
        messages,
        steps,
        openai_tools,
        start_step=start_step,
        max_steps=max_steps,
        selected_skill_ids=selected,
    ):
        yield event


def _step_to_dict(record: ReactStepRecord) -> dict[str, Any]:
    return {
        "step": record.step,
        "thought": record.thought,
        "action": record.action,
        "action_input": record.action_input,
        "observation": record.observation,
    }


def steps_from_dicts(items: list[dict[str, Any]]) -> list[ReactStepRecord]:
    return [
        ReactStepRecord(
            step=item.get("step", 0),
            thought=item.get("thought", ""),
            action=item.get("action", ""),
            action_input=item.get("action_input") or {},
            observation=item.get("observation"),
        )
        for item in items
    ]


async def stream_final_answer(
    payload: AgentChatRequest,
    steps: list[ReactStepRecord],
    draft_answer: str | None,
    temperature: float,
) -> AsyncIterator[str]:
    """推理模型未产出正文时，用对话模型基于工具结果生成回答。"""
    locale = get_locale_from_context(payload.context)
    tool_summary = json.dumps(
        [_step_to_dict(s) for s in steps],
        ensure_ascii=False,
    )
    user_content = (
        f"用户问题：{payload.get_current_user_content()}\n\n"
        f"工具调用记录：{tool_summary}\n\n"
        "请根据工具返回结果，向用户输出完整回答；"
        "正文与思考过程使用与用户语言偏好一致的语言；"
        "若你认为有助于 FAE 复盘，可在文末自行决定是否用 Mermaid 概括执行过程。"
    )
    if draft_answer:
        user_content += f"\n\n参考草稿：{draft_answer}"

    messages = [
        {
            "role": "system",
            "content": build_final_answer_system_prompt(locale),
        },
        *_build_history_messages(payload),
        {"role": "user", "content": user_content},
    ]

    generation_id = generation_id_from_context(payload.context)
    async for delta in iter_completion_deltas(
        messages,
        temperature=temperature,
        generation_id=generation_id,
    ):
        yield delta
