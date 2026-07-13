---
name: bag-upload
description: 查询或切换设备 Bag 日志立即上传开关。用户询问 Bag 开关状态或要求打开/关闭 Bag 上传时使用。
tools:
  - get_bag_upload_status
  - switch_bag_upload
react_hint: Bag 上传：查询 get_bag_upload_status；切换 switch_bag_upload（切换操作需用户确认）
casual_capability: get_bag_upload_status / switch_bag_upload：Bag 日志立即上传开关查询与操作
keywords:
  - bag
  - bag开关
  - bag上传
  - 日志上传
  - 立即上传
---

# Bag 日志上传

用户查询机器 Bag 日志上传开关状态 → `get_bag_upload_status`（upload_status=true 表示已开启立即上传）。

用户打开或关闭 Bag 日志立即上传开关 → `switch_bag_upload`（upload_status=true 打开，false 关闭；切换需用户确认）。
