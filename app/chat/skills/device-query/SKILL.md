---
name: device-query
description: 按 SN/DID 查询单台设备信息（固件、在线、激活等）或 OTA 升级路线。用户提供设备 SN/DID 并询问设备状态时使用。
tools:
  - get_robot_info
  - get_ota_router
react_hint: 单台设备：get_robot_info；OTA 路线：get_ota_router
casual_capability: get_robot_info：按 SN/DID 查单台设备（固件、在线、激活等）；get_ota_router：OTA 升级路线与各版本升级状态
keywords:
  - 固件版本
  - 在线
  - 激活
  - ota
  - 升级路线
  - 升级路径
  - 升级进度
  - 能升到
---

# 单台设备查询

用户提供 SN/DID 查询单台设备信息（固件版本、激活/在线状态等）→ `get_robot_info`。

查看 OTA 升级路线与各版本升级状态 → `get_ota_router`。

SN 示例：YXCAAM263HXB05N1105；DID 示例：82eb290783b2480384e74d6f13755e78。
