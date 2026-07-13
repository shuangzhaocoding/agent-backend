---
name: knowledge
description: 检索售后知识库，适用于故障现象、排查步骤、使用说明。用户询问产品故障、排查方法、使用方法时使用。
tools:
  - get_error_code_info
  - get_voice_code_info
  - search_knowledge
react_hint: 系统错误码：get_error_code_info；语音错误码：get_voice_code_info；故障/排查/使用说明：search_knowledge；未命中或信息不足时可改查 product-attrs 中的产品参数工具链
casual_capability: get_error_code_info：系统错误码与 UI 码映射；get_voice_code_info：语音错误码详情；search_knowledge：售后知识库（故障现象、排查步骤、使用说明）
keywords:
  - 故障
  - 排查
  - 使用说明
  - 怎么办
  - 如何
  - 溢水
  - 关机
  - 报错
  - 错误码
  - 0x
  - appcode
  - ui错误码
  - 语音
  - voice
---

# 知识库检索

产品故障、排查、使用说明 → `search_knowledge`（售后知识库）。

## 错误码检索

系统错误码为 **16 进制**，格式如 `0x02100018` 或省略前导零的 `0x2100018`（`0x` + 7~8 位十六进制，库内统一为 8 位）。APP 端展示的 **4 位纯数字**（如 `2003`）为 UI 错误码（appCode）。

1. **先查错误码详情**：`get_error_code_info`
   - 传入十六进制 `code`（如 `0x02100018`）或 4 位 `appCode`（如 `2003`）
   - UI 错误码会先查 `error_code_info` 映射为十六进制 `code`
2. **语音播报查询**：`get_voice_code_info`
   - 传入 `voice451`、`voiceK16` 等语音码，或关联的十六进制 / APP UI 错误码
   - 返回机器人语音文案（`source_content`）、关联 `error_code` / `app_code` 等
3. **再查 SOP**：`search_knowledge`，`query` 中带上映射后的十六进制错误码

- 7 位写法会自动补零为 `0x02100018` 参与匹配
- 知识库会按错误码关联 SOP 精确过滤，再向量召回与重排
- 大小写不敏感（`0X02100018` 会规范为 `0x02100018`）

## 资源链接识别

`search_knowledge` 返回的 `hits[].content` 与 `summary` 中可能包含已格式化的资源链接，回答用户时须识别并完整保留：

| 类型 | 格式示例 | 处理方式 |
|------|----------|----------|
| 图片 | `![描述](https://...jpg)` | 原样输出 Markdown 图片 |
| 视频 | `<video controls><source src="https://...mp4"></video>` | 原样输出 video 标签 |
| 文档/文件 | `[文件名](https://...pdf)` 或 `<a href="https://...">` | 原样输出链接 |

若知识库未命中或结果不足以回答，可改查 **product-attrs** skill 中的产品参数工具链，补充规格/能力说明。
