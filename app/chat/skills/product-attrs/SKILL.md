---
name: product-attrs
description: 查询产品参数、规格、型号对比与全库筛选。用户询问吸力、尺寸、续航、型号对比或未指定型号的条件筛选时使用。
tools:
  - list_product_catalog
  - list_attr_fields
  - get_product_attrs
  - scan_product_attrs
react_hint: 产品参数：list_product_catalog、list_attr_fields、get_product_attrs、scan_product_attrs；未命中时可改查 search_knowledge
casual_capability: 产品参数：list_product_catalog、list_attr_fields、get_product_attrs、scan_product_attrs（型号目录、参数字段、按型号查参、全库筛选对比）
keywords:
  - 参数
  - 规格
  - 吸力
  - 尺寸
  - 重量
  - 电池
  - 续航
  - 对比
  - 多大
  - 多重
  - 容量
  - 噪音
  - 尘盒
  - 水箱
---

# 产品参数

产品参数、规格、型号对比 → 产品参数工具链：

- `list_product_catalog`：查型号目录与别名
- `list_attr_fields`：查参数字段说明（field_key / label）
- `get_product_attrs`：按规范型号查参数（可指定 field_keys）
- `scan_product_attrs`：全库按字段扫描（条件筛选/排序，须先 list_attr_fields）

（不包括三码/四码、MES 关联 SN、单台设备固件/在线/OTA 查询）

若参数库未查到或无法回答用户问题，可改查 **knowledge** skill 中的 search_knowledge，补充故障排查与使用说明。
