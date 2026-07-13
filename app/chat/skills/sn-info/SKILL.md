---
name: sn-info
description: 查询设备三码、四码或关联 SN。用户询问 J1/J2/J3+ 三码四码、包装 SN、基站 SN 等关联码时使用。
tools:
  - get_sn_info
  - get_sn_info_j1
  - get_sn_info_j2
react_hint: 三码四码：J3+ 用 get_sn_info；J1 用 get_sn_info_j1；J2 用 get_sn_info_j2
casual_capability: get_sn_info / get_sn_info_j1 / get_sn_info_j2：三码、四码、关联 SN（J1/J2/J3+）
keywords:
  - 三码
  - 四码
  - 关联sn
  - 包装sn
  - 基站sn
  - 机器码
---

# 三码 / 四码

用户查询设备三码、四码或关联 SN：

- J3 及以后新款（MES）→ `get_sn_info`（SN 如 YXCAAM263HXB05N1105）
- J1 → `get_sn_info_j1`（返回 robot_sn、station_sn、package_sn、robot_cid）
- J2 → `get_sn_info_j2`（返回 robot_sn、station_sn、package_sn）
