---
name: nw-config
description: 查询设备 BLE 配网记录与配网历史。用户询问配网记录、配网历史、连网失败记录时使用。
tools:
  - get_nw_config_history
  - get_app_logs
react_hint: 配网记录：get_nw_config_history（key 可为 SN、DID、uid、uuid）；APP 日志：get_app_logs（需 APP 用户 ID/uid 或 uuid，返回 OBS 下载链接）
casual_capability: get_nw_config_history：配网记录查询（SN/DID/uid/uuid）；get_app_logs：APP 客户端日志（OBS 下载）
keywords:
  - 配网记录
  - 配网历史
  - 连网失败
  - 配网
---

# 配网记录

用户查询配网记录、配网历史 → `get_nw_config_history`（key 可为 SN、DID、uid、uuid）。
APP 侧配网过程日志 → `get_app_logs`（需 APP 用户 ID/uid 或 uuid；返回 OBS 下载链接）。
