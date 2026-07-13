---
name: create-jira
description: 一键提 Jira 工单、建缺陷单、反馈问题。用户要提单、建工单，或提供问题描述 + SN/DID + 发生时间时使用；由专用 workflow 处理，非 ReAct 工具。
workflow: create_jira
react_hint: create_jira 由独立 workflow 处理，ReAct 模式下勿调用工具模拟提单
casual_capability: create_jira：一键提 Jira 工单（问题描述 + SN + 发生时间）
keywords:
  - 提单
  - 提jira
  - 建jira
  - 创建jira
  - jira单
  - jira工单
  - 一键提单
  - 反馈问题
  - 建缺陷
  - 创建工单
  - 提交工单
  - 【问题描述】
  - 【sn】
  - 问题发生时间
---

# 创建 Jira 工单

用户要提 Jira 单、建工单、反馈缺陷、一键提单 → **create_jira**（专用 workflow，非上述 ReAct 工具）。

系统会进入提单 workflow：解析问题描述、SN/DID、发生时间，经用户确认后创建 Jira 工单。

## 输入要求

提单输入示例（问题描述 + SN + 发生时间，可为自由文本或【标签】格式）：

- 基站溢水，重启无效 YXCAAM2629XB05N0097 时间：2026-06-04 03:01:11.035
- 回洗拖布后返回途中拖布一直工作中，应该是抬起拖布，到拖地点再放下拖布
  YFEAAM2593XB00C0098 2026-06-16 03:01:11.035

## ReAct 模式说明

若当前处于 ReAct 循环且已加载本 Skill，说明用户意图为提单，**勿调用任何工具模拟提单**；提单由独立 workflow 负责。
