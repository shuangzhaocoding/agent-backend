# -*- coding: utf-8 -*-
#
# Workflow 公共类型与流式回答
#
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Optional

from chat.generation_cancel import generation_id_from_context
from chat.llm import iter_completion_deltas
from chat.react import ReactEvent, ReactStepRecord, stream_plain_text
from chat.schemas import AgentChatRequest

WorkflowMode = Literal[
    "workflow_create_jira",
    "workflow_query_product_attrs",
]


@dataclass
class WorkflowRunResult:
    mode: WorkflowMode
    intent: str
    steps: list[ReactStepRecord] = field(default_factory=list)
    final_answer: str = ""
    workflow_data: dict[str, Any] = field(default_factory=dict)


async def stream_workflow_answer(
    payload: AgentChatRequest,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
) -> AsyncIterator[str]:
    messages = [
        {"role": "system", "content": system_prompt},
        *payload.get_history_messages(),
        {"role": "user", "content": user_prompt},
    ]
    generation_id = generation_id_from_context(payload.context)
    async for delta in iter_completion_deltas(
        messages,
        temperature=temperature,
        generation_id=generation_id,
    ):
        yield delta


async def emit_workflow_finish(
    payload: AgentChatRequest,
    result: WorkflowRunResult,
    system_prompt: Optional[str] = None,
    user_prompt: Optional[str] = None,
) -> AsyncIterator[ReactEvent]:
    """产出 workflow 完成事件；若未预设 final_answer 则生成回答文本。"""
    draft = (result.final_answer or "").strip()
    temperature = payload.temperature or 0.7
    full = draft

    if not full:
        if system_prompt and user_prompt:
            async for delta in stream_workflow_answer(
                payload,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
            ):
                full += delta
        else:
            async for delta in stream_workflow_answer(
                payload,
                system_prompt=(
                    "你是云鲸产品技术支持助手。根据 workflow 执行结果向用户输出完整中文回答，"
                    "条理清晰，面向 FAE/客服；不要暴露内部字段名。"
                ),
                user_prompt=(
                    f"用户问题：{payload.get_current_user_content()}\n\n"
                    f"执行结果：{json.dumps(result.workflow_data, ensure_ascii=False)}"
                ),
                temperature=temperature,
            ):
                full += delta

    yield ReactEvent(
        type="finish",
        step=len(result.steps) or 1,
        data={
            "final_answer": full,
            "steps": [
                {
                    "step": s.step,
                    "thought": s.thought,
                    "action": s.action,
                    "action_input": s.action_input,
                    "observation": s.observation,
                }
                for s in result.steps
            ],
            "mode": result.mode,
            "intent": result.intent,
            "workflow_data": result.workflow_data,
        },
    )
