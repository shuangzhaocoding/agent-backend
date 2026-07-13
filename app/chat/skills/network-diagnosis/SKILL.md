---
name: network-diagnosis
description: 查询设备离线/网络诊断（WiFi、AP、外网、DNS、云鲸服务连接等）。用户询问离线诊断、网络诊断、WiFi 连接问题时使用。
tools:
  - device_network_diagnosis
  - get_app_logs
react_hint: 网络诊断：device_network_diagnosis（默认近 7 天；需 DID/SN）；APP 日志：get_app_logs（需 APP 用户 ID/uid 或 uuid，返回 OBS 下载链接）
casual_capability: device_network_diagnosis：离线/网络诊断（WiFi、AP、外网、DNS 等）；get_app_logs：APP 客户端日志（OBS 下载）
keywords:
  - 离线诊断
  - 网络诊断
  - wifi
  - ping
  - mqtt
  - dns
---

# 网络诊断

用户查询设备离线/网络诊断（WiFi、AP、外网、DNS、云鲸服务连接等）→ `device_network_diagnosis`（默认近 7 天；需 DID/SN）。
APP 侧配网/连网日志 → `get_app_logs`（需 APP 用户 ID/uid 或 uuid，默认近 7 天；返回 OBS 下载链接）。
