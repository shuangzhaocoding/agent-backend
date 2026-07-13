---
name: firmware-market
description: 查询某机型最新可推送市场版本与固件版本库（非单台设备）。用户询问某型号或某SN最新固件、市场版本、可推送版本时使用。
tools:
  - get_sn_info
  - get_robot_info
  - push_firmware
  - list_product_catalog
  - get_firmware_infos
react_hint: 固件市场版本：先 list_product_catalog 确认 product_name，再 get_firmware_infos；product 须为目录中的规范 product_name，禁止传别名
casual_capability: get_firmware_infos：某机型最新可推送市场版本与固件版本库（须先 list_product_catalog 将别名映射为 product_name）
keywords:
  - 最新固件
  - 市场版本
  - 可推送
  - 固件版本库
  - 推送版本
---
# 固件市场版本

用户询问某机型最新可推送市场版本、固件推送配置（非单台设备）→

先判断用户给的是机器人型号（如： 逍遥003）还是设备SN（如： YF5AAM254TXB00N1646）

**如果是机器人型号：**

* 1、先 `list_product_catalog` 确认规范 product_name
* 2、再以该 product_name 作为 `get_firmware_infos` 的 product 参数查询（配合 country，如 J4 + cn）

**如果是设备SN:**

- 1、通过工具`get_sn_info`获取机器人SN
- 2、通过工具`get_robot_info`获取机器人产品名称
- 3、通过 `get_firmware_infos`获取firmware_id以及可推送的最新版本

禁止用用户简称、别名或口语型号直接查 get_firmware_infos，须从目录 aliases 映射为 product_name 后再传。