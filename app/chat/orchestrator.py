# -*- coding: utf-8 -*-
#
# Agent 编排：统一 Skill 路由 + ReAct（含 workflow Skill 分支）
#
from typing import AsyncIterator

from chat.react import ReactEvent, run_react_loop
from chat.schemas import AgentChatRequest


async def run_agent_orchestrator(
    payload: AgentChatRequest,
    intent_result=None,
) -> AsyncIterator[ReactEvent]:
    """统一入口：Skill 路由 → ReAct / workflow（create-jira 等）。"""
    del intent_result  # 保留参数兼容旧调用方，已不再使用意图分类
    async for event in run_react_loop(payload):
        yield event
