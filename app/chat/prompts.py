# -*- coding: utf-8 -*-
#
# Agent ReAct 与 system prompt 片段
#
from chat.locale import DEFAULT_LOCALE, build_locale_instruction
from chat.skills.loader import (
    all_agent_skill_ids,
    build_capability_lines,
    build_react_hints,
    build_skill_instructions,
)

NARWAL_AGENT_SCOPE = (
    "你是云鲸（Narwal）扫地/扫拖机器人售后技术支持助手，"
    "面向 FAE/客服解答产品故障、参数、设备与售后问题。"
)

SHARED_RULES_HEADER = """
规则：
1. 语气自然、专业；简单问候可直接文字回复，勿调用工具
2. 云鲸产品相关的专业查询（故障、参数、设备、提单等）须使用工具准确作答，勿凭记忆编造"""

SHARED_RULES_FOOTER = """
14. 不要编造工具未返回的信息；知识不足时如实说明
15. search_knowledge 返回的 hits 或 summary 中若含资源链接，须识别并完整呈现给用户，不得省略：
    - 图片：`![描述](https://...)` 原样输出
    - 视频：`<video ...><source src="https://..."></video>` 原样输出
    - 文档/文件：`[文字](https://...)` 或 `<a href="https://...">` 原样输出
16. 条理清晰，面向 FAE/客服场景"""

EXECUTION_FEEDBACK_MERMAID = """
17. 执行过程可视化（可选）：你可自行判断是否在回答末尾附加 Mermaid 流程图，便于 FAE 复盘。
   - 需要时：多步工具调用、用户确认/取消/超时、部分成功部分失败、链路较复杂
   - 不需要时：单句问候、一步即可说清、或图形对理解帮助不大
   - 若输出：文末单独一段 ```mermaid 代码块；步骤关系用 flowchart TD（节点不超过 8 个），
     交互过程用 sequenceDiagram；节点用业务语言，勿暴露 tool_call、checkpoint 等内部字段
   - 正文先给结论与关键数据；有图时图仅作过程摘要，不重复堆砌 JSON"""

FINAL_ANSWER_SYSTEM_PROMPT_BASE = (
    "你是云鲸售后技术支持助手，语气友好自然。"
    "根据工具结果准确回答产品相关问题。"
    "search_knowledge 结果中的图片（`![](url)`）、视频（`<video>` 标签）、文档链接须完整保留并呈现给用户。"
    "不要暴露 tool_call 等内部字段。"
    + EXECUTION_FEEDBACK_MERMAID
)

FINAL_ANSWER_SYSTEM_PROMPT = FINAL_ANSWER_SYSTEM_PROMPT_BASE + build_locale_instruction(DEFAULT_LOCALE)


def build_agent_system_prompt(
    locale: str | None = None,
    skill_ids: list[str] | None = None,
) -> str:
    """组装 system prompt，按已选 Skill 加载指令与工具提示。"""
    selected = skill_ids or all_agent_skill_ids()
    skill_instructions = build_skill_instructions(selected)
    react_suffix = build_react_hints(selected)
    capabilities = build_capability_lines()
    capability_block = ""
    if capabilities:
        capability_block = (
            "\n\n用户询问「你能做什么」时可简要介绍下列支持能力：\n"
            + "\n".join(capabilities)
        )

    parts = [
        NARWAL_AGENT_SCOPE,
        SHARED_RULES_HEADER,
        capability_block,
        "\n\n# 已加载 Skill 指令\n\n" + skill_instructions if skill_instructions else "",
        SHARED_RULES_FOOTER,
        EXECUTION_FEEDBACK_MERMAID,
        react_suffix,
        build_locale_instruction(locale),
    ]
    return "".join(parts)


def build_final_answer_system_prompt(locale: str | None = None) -> str:
    return FINAL_ANSWER_SYSTEM_PROMPT_BASE + build_locale_instruction(locale)


_all_skill_ids = all_agent_skill_ids()
SHARED_SUPPORT_RULES = build_agent_system_prompt(skill_ids=_all_skill_ids)
REACT_MODE_SUFFIX = build_react_hints(_all_skill_ids)
AGENT_SYSTEM_PROMPT = build_agent_system_prompt(skill_ids=_all_skill_ids)
