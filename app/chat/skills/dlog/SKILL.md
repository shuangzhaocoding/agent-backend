---
name: dlog
description: 下载并分析设备 Dlog 日志。用户要求分析设备日志、查看 dlog、排查日志故障时使用；需 SN/DID 与时间范围。
tools:
  - get_dlog_data
react_hint: Dlog 日志：get_dlog_data（需 SN/DID 与时间范围，默认近 7 天）
casual_capability: get_dlog_data：Dlog 日志分析（需 SN/DID 与时间范围）
keywords:
  - dlog
  - 设备日志
  - 日志分析
  - 分析日志
  - 下载日志
---

# Dlog 日志

用户分析设备 Dlog 日志、排查日志故障（需 DID/SN 与时间范围）→ `get_dlog_data`（默认近 7 天）。

DID 示例：82eb290783b2480384e74d6f13755e78。
