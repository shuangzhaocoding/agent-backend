---
name: firmware-push
description: 向单台设备推送固件、单点 OTA 升级。用户要求推送固件或升级设备到指定版本时使用；执行前须用户在前端确认。
tools:
  - get_sn_info
  - get_robot_info
  - get_firmware_infos
  - push_firmware
react_hint: 固件推送：push_firmware（需用户前端确认；先 get_firmware_infos 获取 firmware_id）
casual_capability: push_firmware：向单台设备推送固件（需用户前端确认；须 firmware_id）
keywords:
  - 推送固件
  - 单点ota
  - 下发固件
  - 升级设备
---
# 固件推送

用户要求向单台设备推送固件、单点 OTA 升级 → `push_firmware`。

- 1、通过工具`get_sn_info`获取机器人SN
- 2、通过工具`get_robot_info`获取机器人产品名称
- 3、通过 `get_firmware_infos`获取firmware_id以及可推送的最新版本

