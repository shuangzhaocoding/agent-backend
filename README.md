# Chat Agent 处理流程说明

本文档描述 `app/chat` 模块中，**当前 Agent 对用户问题的完整处理链路**：入口 API、会话与生成任务存储、Skill 路由、ReAct / Workflow 执行、人机确认、流式续传与长期记忆。文中示例均为示意数据（字段名与真实结构一致）。

路由前缀：`/api/chat`（见 `main.py`）。所有接口需鉴权（`verify_token`）。

---

## 1. 总体架构

```
前端
  │
  ├─ POST /sessions                    → MySQL chat_agent_session
  ├─ POST /agent (session_id+stream)   → 创建 generation → Taskiq IO Worker
  │                                         │
  │                                         ├─ Redis: meta / chunks / payload / checkpoint
  │                                         ├─ 进程内 Buffer（Worker 侧）
  │                                         └─ 完成后回写 session.messages + 异步摘要
  │
  ├─ GET  /generations/{id}/stream     → 从 Redis chunks 按 offset 续传 SSE
  ├─ POST /actions/respond             → 确认/取消危险操作 → continue_generation
  └─ POST /agent (无 session)          → API 进程内直连流式 / 非流式
```

| 层级     | 职责                                         | 主要文件                                                             |
| -------- | -------------------------------------------- | -------------------------------------------------------------------- |
| API      | 鉴权、locale、分支（直连 / 后台生成 / 续传） | `router.py`                                                        |
| 会话     | 会话 CRUD、消息合并、评价                    | `session_service.py`                                               |
| 生成任务 | 创建/暂停/完成/取消、SSE 订阅                | `generation_service.py`                                            |
| Worker   | Taskiq 中跑 Agent，写 Redis chunks           | `generation_worker.py` + `async_task_module/tasks/chat_tasks.py` |
| 编排     | Skill 路由 → ReAct 或 Jira Workflow         | `orchestrator.py` → `react.py` / `jira/workflow.py`           |
| 工具     | OpenAI tools 定义与执行                      | `tools/`、`tool_registry.py`                                     |
| 记忆     | 滑动窗口 + 摘要                              | `memory_service.py`、`memory_context.py`                         |

---

## 2. 端到端示例：一次「知识库问答」

以下走生产主路径：`session_id` + `stream=true`。

### 2.1 创建会话

**请求**

```http
POST /api/chat/sessions
Authorization: Bearer <token>
Content-Type: application/json

{
  "title": "J6 基站溢水排查"
}
```

**响应**

```json
{
  "code": 0,
  "message": "会话创建成功",
  "data": {
    "session_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "title": "J6 基站溢水排查",
    "status": 1,
    "messages": [],
    "memory": {
      "summary": "",
      "summarized_until_index": 0
    },
    "creator": "zhangsan",
    "created_at": "2026-07-10 14:00:00",
    "modified_at": "2026-07-10 14:00:00"
  }
}
```

### 2.2 发起对话（只传本轮 user）

**请求**

```http
POST /api/chat/agent
Authorization: Bearer <token>
Accept-Language: zh-CN
Content-Type: application/json

{
  "session_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "stream": true,
  "thinking": true,
  "max_steps": 6,
  "temperature": 0.7,
  "messages": [
    {
      "role": "user",
      "content": "J6 基站溢水怎么办？SN：YXCAAM2629XB05N0097"
    }
  ],
  "files": [
    {
      "name": "overflow.jpg",
      "url": "https://support-fae.obs.cn-south-1.myhuaweicloud.com/agent/xxx/overflow.jpg",
      "size": 3764164,
      "type": "image/jpeg"
    }
  ],
  "context": {
    "region": "cn"
  }
}
```

后端会自动注入：

```json
{
  "region": "cn",
  "locale": "zh-CN",
  "user_email": "zhangsan@narwal.com",
  "username": "zhangsan",
  "_session_memory": {
    "summary": "",
    "summarized_until_index": 0
  }
}
```

**立即返回（不等 LLM）**

```json
{
  "code": 0,
  "message": "生成任务已创建",
  "data": {
    "generation_id": "gen-1111-2222-3333-4444",
    "message_id": "msg-aaaa-bbbb-cccc-dddd",
    "chunk_id": "msg-aaaa-bbbb-cccc-dddd",
    "offset": 0,
    "status": "running",
    "reused": false,
    "celery_task_id": "chat-gen-gen-1111-2222-3333-4444",
    "stream_path": "/api/chat/generations/gen-1111-2222-3333-4444/stream",
    "context_usage": {
      "context_limit": 1048576,
      "recommended_limit": 128000,
      "total_estimated_input": 12580,
      "categories": [
        {"key": "system_prompt", "label": "系统指令", "tokens": 4200},
        {"key": "memory_summary", "label": "长期记忆", "tokens": 0},
        {"key": "recent_messages", "label": "近期对话", "tokens": 0},
        {"key": "current_user", "label": "当前输入", "tokens": 40},
        {"key": "tools_schema", "label": "工具定义", "tokens": 8340}
      ]
    }
  }
}
```

若同一会话已有进行中的 generation，则 `reused: true`，且不重新投递 Taskiq。

### 2.3 订阅 SSE

```http
GET /api/chat/generations/gen-1111-2222-3333-4444/stream?from_offset=0
Authorization: Bearer <token>
```

**典型 SSE 序列（节选）**

```text
# offset=0：generation_info（创建任务时已写入 Redis）
data: {"id":"msg-aaaa-bbbb-cccc-dddd","object":"chat.completion.chunk","created":1720591200,"model":"agent","choices":[{"index":0,"delta":{"generation_id":"gen-1111-2222-3333-4444","message_id":"msg-aaaa-bbbb-cccc-dddd"},"finish_reason":null}]}

# thinking=true：Skill 路由思考
data: {"id":"msg-aaaa-bbbb-cccc-dddd","object":"chat.completion.chunk","created":1720591201,"model":"agent","choices":[{"index":0,"delta":{"reasoning_content":"正在进行技能路由…\n"},"finish_reason":null}]}

data: {"id":"msg-aaaa-bbbb-cccc-dddd","object":"chat.completion.chunk","created":1720591201,"model":"agent","choices":[{"index":0,"delta":{"reasoning_content":"已选择技能：knowledge\n"},"finish_reason":null}]}

# 工具调用思考
data: {"id":"msg-aaaa-bbbb-cccc-dddd","object":"chat.completion.chunk","created":1720591202,"model":"agent","choices":[{"index":0,"delta":{"reasoning_content":"调用工具 search_knowledge…\n"},"finish_reason":null}]}

data: {"id":"msg-aaaa-bbbb-cccc-dddd","object":"chat.completion.chunk","created":1720591203,"model":"agent","choices":[{"index":0,"delta":{"reasoning_content":"工具 search_knowledge 执行完成\n"},"finish_reason":null}]}

# 正文（首包带 role）
data: {"id":"msg-aaaa-bbbb-cccc-dddd","object":"chat.completion.chunk","created":1720591204,"model":"agent","choices":[{"index":0,"delta":{"role":"assistant","content":"根据"},"finish_reason":null}]}

data: {"id":"msg-aaaa-bbbb-cccc-dddd","object":"chat.completion.chunk","created":1720591204,"model":"agent","choices":[{"index":0,"delta":{"content":"知识库，J6 基站溢水可按以下步骤排查：\n1. …"},"finish_reason":null}]}

# 结束前 metadata
data: {"id":"msg-aaaa-bbbb-cccc-dddd","object":"chat.completion.chunk","created":1720591205,"model":"agent","choices":[{"index":0,"delta":{"metadata":{"id":"msg-aaaa-bbbb-cccc-dddd","mode":"react","intent":null,"workflow_data":{},"steps":[{"step":1,"thought":"…","action":"search_knowledge","action_input":{"query":"J6 基站溢水"},"observation":{"summary":"…","hits":[]}}],"context_usage":{"total_estimated_input":15200}}},"finish_reason":null}]}

data: {"id":"msg-aaaa-bbbb-cccc-dddd","object":"chat.completion.chunk","created":1720591205,"model":"agent","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

前端应维护已消费的 `offset`（每收到一条 `data:` 行 +1，含 `[DONE]` 视实现而定；后端 `meta.offset` 为 chunks 列表长度）。刷新后：

```http
GET /api/chat/generations/gen-1111-2222-3333-4444
GET /api/chat/generations/gen-1111-2222-3333-4444/stream?from_offset=12
```

续传时，历史回放结束后可能出现 `live_start` 控制帧：

```json
{
  "choices": [{
    "delta": {
      "metadata": { "type": "live_start", "offset": 12 }
    }
  }]
}
```

---

## 3. 数据存储详解（含 example）

### 3.1 MySQL `chat_agent_session`

#### 生成刚开始时（assistant 占位）

```json
{
  "session_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "title": "J6 基站溢水排查",
  "creator": "zhangsan",
  "status": 1,
  "memory": {
    "summary": "",
    "summarized_until_index": 0
  },
  "messages": [
    {
      "role": "user",
      "content": "J6 基站溢水怎么办？SN：YXCAAM2629XB05N0097"
    },
    {
      "role": "assistant",
      "content": "",
      "metadata": {
        "id": "msg-aaaa-bbbb-cccc-dddd",
        "status": "streaming",
        "generation_id": "gen-1111-2222-3333-4444",
        "blocks": []
      }
    }
  ]
}
```

> 注意：`status=streaming` 的 assistant **不会**进入下一轮 LLM 历史（`is_persisted_conversation_message` 会过滤）。

#### 生成完成后（含 blocks）

```json
{
  "role": "assistant",
  "content": "根据知识库，J6 基站溢水可按以下步骤排查：\n1. 检查清水箱密封…",
  "reasoning_content": "正在进行技能路由…\n已选择技能：knowledge\n调用工具 search_knowledge…\n",
  "metadata": {
    "id": "msg-aaaa-bbbb-cccc-dddd",
    "status": "completed",
    "mode": "react",
    "intent": null,
    "blocks": [
      {
        "type": "reasoning",
        "content": "正在进行技能路由…\n已选择技能：knowledge\n调用工具 search_knowledge…\n工具 search_knowledge 执行完成\n"
      },
      {
        "type": "text",
        "content": "根据知识库，J6 基站溢水可按以下步骤排查：\n1. 检查清水箱密封…"
      }
    ],
    "workflow_data": {},
    "steps": [
      {
        "step": 1,
        "thought": "用户问基站溢水，应检索知识库",
        "action": "search_knowledge",
        "action_input": { "query": "J6 基站溢水" },
        "observation": {
          "summary": "…",
          "hits": [
            {
              "title": "基站溢水排查",
              "score": 0.91,
              "content": "…"
            }
          ]
        }
      }
    ]
  }
}
```

> 流式中间态主要写 Redis；MySQL 在 **pause / finalize / cancel** 时通过 `sync_session_assistant_from_generation` 回写。`final=true` 时会去掉 `generation_id`，并把 `status` 置为 `completed` / `cancelled` 等。

#### 多轮后的 `memory` example

```json
{
  "summary": "用户设备 SN=YXCAAM2629XB05N0097，型号 J6。已反馈基站溢水，知识库建议检查清水箱密封与回水管路。用户尚未确认是否建 Jira。",
  "summarized_until_index": 8,
  "updated_at": "2026-07-10 15:30:00"
}
```

含义：`messages[0:8]` 已折叠进摘要；送入 LLM 时用摘要前缀 + 最近 12 条原文。

### 3.2 MySQL `chat_agent_message_feedback`

**请求**

```json
{
  "session_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "message_id": "msg-aaaa-bbbb-cccc-dddd",
  "vote": -1,
  "category": "inaccurate",
  "comment": "排查步骤与机型不符"
}
```

**落库示意**

```json
{
  "session_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "message_id": "msg-aaaa-bbbb-cccc-dddd",
  "vote": -1,
  "category": "inaccurate",
  "comment": "排查步骤与机型不符",
  "feedback_by": "zhangsan",
  "feedback_at": "2026-07-10 14:10:00"
}
```

`vote`：`1` 点赞 / `-1` 点踩 / `0` 取消。

### 3.3 Redis 键与内容 example

实现见 `redis_generation.py`。假设配置 `key_prefix = chat:gen`（见 `config.yaml`），`generation_id = gen-1111-2222-3333-4444`，`creator = zhangsan`，`session_id = a1b2c3d4-…`。各 key 默认 TTL 与 `ttl_seconds` 一致（如 86400）。

| Key | Redis 类型 | 值格式 | 核心作用 |
|-----|------------|--------|----------|
| `chat:gen:{gid}:meta` | Hash | 字段多为字符串；`workflow_data`/`steps`/`blocks` 为 JSON 字符串 | 任务状态与聚合字段 |
| `chat:gen:{gid}:chunks` | List | 元素为完整 SSE 行（`data: {...}\n\n`） | 断线续传内容，下标即 offset |
| `chat:gen:{gid}:payload` | String | JSON（`AgentChatRequest`） | Worker 读取的完整请求 |
| `chat:gen:{gid}:checkpoint` | String | JSON 对象 | paused 现场，确认/超时后续跑 |
| `chat:gen:{gid}:signal` | List | 元素固定 `"1"`（无业务含义） | `BRPOP` 唤醒 SSE 订阅方 |
| `chat:gen:{gid}:cancel_requested` | String | 固定 `"1"`，**存在即表示已请求停止** | 协作式取消旗标 |
| `chat:gen:session_running:{creator}:{session_id}` | String | 当前 `generation_id` | 会话级互斥 / 复用 |

关系：

```
session_running ──指向──► generation_id
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
           checkpoint      signal         cancel_requested
           (暂停现场)    (List 唤醒)      (停止旗标 "1")
                              │
                              ▼
                           chunks（真正 SSE 内容）
```

#### `meta` Hash example（running）

```json
{
  "generation_id": "gen-1111-2222-3333-4444",
  "session_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "message_id": "msg-aaaa-bbbb-cccc-dddd",
  "chunk_id": "msg-aaaa-bbbb-cccc-dddd",
  "creator": "zhangsan",
  "status": "running",
  "offset": "15",
  "content": "根据知识库，J6…",
  "reasoning_content": "正在进行技能路由…",
  "mode": "react",
  "intent": "",
  "workflow_data": "{}",
  "steps": "[]",
  "blocks": "[{\"type\":\"reasoning\",\"content\":\"…\"},{\"type\":\"text\",\"content\":\"根据知识库…\"}]",
  "celery_task_id": "chat-gen-gen-1111-2222-3333-4444",
  "created_at": "2026-07-10 14:00:01",
  "modified_at": "2026-07-10 14:00:08"
}
```

> `workflow_data` / `steps` / `blocks` 在 Redis 中以 JSON 字符串存储，读取时再反序列化。

#### `payload` example（Worker 入参）

```json
{
  "messages": [
    { "role": "user", "content": "J6 基站溢水怎么办？SN：YXCAAM2629XB05N0097" }
  ],
  "stream": true,
  "thinking": true,
  "temperature": 0.7,
  "max_steps": 6,
  "session_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "files": [
    {
      "name": "overflow.jpg",
      "url": "https://support-fae.obs.cn-south-1.myhuaweicloud.com/agent/xxx/overflow.jpg",
      "size": 3764164,
      "type": "image/jpeg"
    }
  ],
  "context": {
    "region": "cn",
    "locale": "zh-CN",
    "user_email": "zhangsan@narwal.com",
    "username": "zhangsan",
    "_session_memory": {
      "summary": "",
      "summarized_until_index": 0
    },
    "_generation_id": "gen-1111-2222-3333-4444"
  }
}
```

> `_generation_id` 由 Worker 注入，用于工具执行中检查是否已取消。

#### `checkpoint`（String / JSON）— 暂停检查点

**写入**：`pause_generation` → `save_generation_checkpoint`（`SET key json EX ttl`）。

**读取**：用户 `POST /actions/respond`、超时任务 `chat.expire_generation_action`、续跑 Worker `continue_generation_async`。

**原理**：遇到需确认工具 / 建 Jira 时 Worker 不能阻塞等用户；把 ReAct 消息链与 pending tool 序列化进 Redis，`meta.status=paused`，并投递超时任务。确认后把 `user_response` 写回同一 checkpoint，再投递 `chat.continue_generation` 恢复执行。

```json
{
  "action_id": "act-5555-6666-7777-8888",
  "action_type": "push_firmware",
  "draft": { "sn": "YXCAAM2629XB05N0097", "version": "4.5.6" },
  "kind": "react",
  "preview_text": "即将执行工具「push_firmware」，参数如下：…",
  "mode": "react",
  "intent": null,
  "steps": [],
  "react_messages": [
    { "role": "system", "content": "…" },
    { "role": "user", "content": "帮这台机器推送固件 4.5.6" },
    {
      "role": "assistant",
      "content": "",
      "tool_calls": [
        {
          "id": "call_push1",
          "type": "function",
          "function": {
            "name": "push_firmware",
            "arguments": "{\"sn\":\"YXCAAM2629XB05N0097\",\"version\":\"4.5.6\"}"
          }
        }
      ]
    }
  ],
  "pending_tool_call": {
    "id": "call_push1",
    "name": "push_firmware",
    "arguments": "{\"sn\":\"YXCAAM2629XB05N0097\",\"version\":\"4.5.6\"}"
  },
  "remaining_tool_calls": [
    {
      "id": "call_push1",
      "name": "push_firmware",
      "arguments": "{\"sn\":\"YXCAAM2629XB05N0097\",\"version\":\"4.5.6\"}"
    }
  ],
  "step_idx": 1,
  "selected_skill_ids": ["firmware-push"],
  "direct_reply": false,
  "confirm_timeout_sec": 300,
  "paused_at": 1720591800,
  "expires_at": 1720592100,
  "user_response": {
    "approved": true,
    "draft": { "sn": "YXCAAM2629XB05N0097", "version": "4.5.6" }
  }
}
```

> `user_response` 仅在用户 respond（或超时路径写入等价取消）后出现。workflow 类（`create_jira`）结构类似，`kind=workflow`，`react_messages` 可为空。

#### `signal`（List）— SSE 唤醒信号

**类型**：Redis `LIST`，元素几乎都是字面量 `"1"`，**不承载业务数据**。

**写入**：
- `append_chunks_batch`：新 SSE 行写入 `chunks` 后 `RPUSH signal "1"`
- `notify_generation_signal`：状态变化 / 取消时主动唤醒

**读取**：`stream_generation_chunks` 实时阶段 `wait_signal` → `BRPOP signal timeout`

**原理**（生产者-消费者，避免空转轮询）：

```
Worker 写 chunks ──RPUSH──► signal list
前端 GET /stream 卡在 BRPOP ◄── 被唤醒后 LRANGE chunks[seen_len:]
```

未使用 Pub/Sub：用阻塞弹出空信号表示「有变化」；真正内容始终在 `chunks`。取消时也会 `RPUSH` 一次，让卡在 `BRPOP` 的连接立刻醒来检查 `cancel_requested` / 终态。

#### `cancel_requested`（String）— 协作停止旗标

**写入**：`POST /generations/{id}/cancel` → `request_generation_cancel` → `SET … "1" EX ttl`，并 `notify_generation_signal`。

**读取**：Worker / 工具执行前 `is_generation_cancelled`（`generation_cancel.py`）：
1. 先查进程内 `_local_cancelled` 集合（同 Worker 免打 Redis）
2. 再 `GET cancel_requested`；命中则写入本地缓存

**原理**：协作式取消，不是强杀进程。

```
API 设 cancel_requested="1" + 唤醒 signal
  → Worker 循环 / 工具前检查
  → abort_generation_as_cancelled（保留已输出，status=cancelled）
  → 清掉 cancel 标记与本地缓存
```

值固定为 `"1"`，**key 存在即表示已请求停止**；不存取消原因。

#### `session_running`（String）— 会话互斥

**Key**：`{prefix}:session_running:{creator}:{session_id}`  
**Value**：当前 `generation_id` 字符串。

| Key | Value |
|-----|--------|
| `chat:gen:session_running:zhangsan:a1b2c3d4-…` | `gen-1111-2222-3333-4444` |

**生命周期**：
- 创建 generation：`set_session_running`
- 完成 / 失败 / 取消：`clear_session_running`（`finalize` / `abort`）
- **`paused` 不清除**：会话仍视为有进行中任务

**原理**：同一用户同一会话只允许一个活跃 generation。

```
POST /agent 再次进来
  → get_session_running(creator, session_id)
  → 若有且 status ∈ {running, paused} → reused=true，不新建、不投递
  → 否则新建 generation 并 SET 覆盖该 key
```

用 `(creator, session_id)` 做命名空间，避免跨用户串会话。

#### 状态机

```
running ──pause──► paused ──respond/expire──► running ──► completed
   │                  │
   └──── cancel ──────┴──────────────────────────────► cancelled
                                              失败时 ► failed
```

---

## 4. 用户问题进入后的三条路径

### 4.1 带 `session_id` + `stream=true`（生产主路径）

见第 2 节。要点：

1. 校验会话归属；
2. 复用已有 running/paused generation；
3. 合并历史 → 注入 memory → 写 assistant 占位 → Redis meta/payload → Taskiq `chat.run_generation`；
4. 立即返回 `generation_id`，前端拉 SSE。

### 4.2 无 `session_id` + `stream=true`（直连）

```json
{
  "stream": true,
  "thinking": false,
  "messages": [{ "role": "user", "content": "你好" }]
}
```

API 进程内直接 `StreamingResponse(run_agent_stream(...))`，**不写 MySQL / Redis generation**，断线即丢。

### 4.3 `stream=false`（非流式）

**请求**

```json
{
  "stream": false,
  "messages": [{ "role": "user", "content": "J6 最大吸力是多少" }],
  "context": { "skill_ids": ["product-attrs"] }
}
```

**响应 data example**

```json
{
  "content": "J6 最大吸力为 12000Pa。",
  "mode": "react",
  "intent": null,
  "workflow_data": {},
  "thinking": false,
  "reasoning_content": null,
  "steps": [
    {
      "step": 1,
      "thought": "",
      "action": "get_product_attrs",
      "action_input": {
        "product_names": ["J6"],
        "fields": ["suction"],
        "region": "cn"
      },
      "observation": {
        "products": [
          { "product_name": "J6", "suction": "12000Pa" }
        ]
      }
    }
  ]
}
```

若在确认点暂停（非流式）：

```json
{
  "paused": true,
  "content": "即将创建 Jira 工单，请确认…",
  "mode": "workflow_create_jira",
  "intent": "create_jira",
  "workflow_data": {
    "status": "paused",
    "action_id": "act-xxxx",
    "action_type": "create_jira",
    "title": "创建 Jira 工单",
    "draft": { "product_type": "J6", "description": "基站溢水", "sn": "YXCAAM2629XB05N0097" },
    "kind": "workflow",
    "respond_api": {
      "method": "POST",
      "path": "/api/chat/actions/respond",
      "description": "用户确认或取消后调用；approved=true 时可将修改后的 draft 放入 draft 字段"
    },
    "confirm_timeout_sec": 300,
    "paused_at": 1720591300,
    "expires_at": 1720591600
  },
  "checkpoint": { "...": "见第 6 节" },
  "steps": []
}
```

### 4.4 其它入口

| 接口                          | 用途                                  |
| ----------------------------- | ------------------------------------- |
| `POST /agent-debug`         | 始终进程内直连，忽略 session 后台生成 |
| `resume_generation_id`      | 兼容旧客户端，等价订阅已有 SSE        |
| `POST /context-usage`       | 发送前预估 token                      |
| `POST /suggested-questions` | 生成「你可能还想问」                  |

**suggested-questions 请求/响应 example**

```json
// 请求
{
  "messages": [
    { "role": "user", "content": "J6 吸力多少" },
    { "role": "assistant", "content": "J6 最大吸力为 12000Pa…" }
  ],
  "count": 3
}

// 响应 data
{
  "title": "你可能还想问",
  "questions": [
    "J6 和 J5 吸力差多少？",
    "J6 续航多久？",
    "如何查看当前固件版本？"
  ],
  "suggested_questions": [
    "J6 和 J5 吸力差多少？",
    "J6 续航多久？",
    "如何查看当前固件版本？"
  ]
}
```

---

## 5. 交互逻辑：从提问到回答

### 5.1 请求预处理

1. **Locale**：`Accept-Language` / `language` → `context.locale`
2. **用户身份**：token → `user_email` / `username`
3. **消息合并**（`merge_session_request_messages`）

**合并 example**

DB 已有：

```json
[
  { "role": "user", "content": "查一下这台机器信息" },
  {
    "role": "assistant",
    "content": "该设备型号为 J6…",
    "metadata": { "id": "msg-old", "status": "completed", "blocks": [...] }
  }
]
```

前端本轮只传：

```json
[{ "role": "user", "content": "再帮我查一下 OTA 升级路线" }]
```

合并后进入 Agent 的 `messages`：

```json
[
  { "role": "user", "content": "查一下这台机器信息" },
  { "role": "assistant", "content": "该设备型号为 J6…", "metadata": { "..." : "..." } },
  { "role": "user", "content": "再帮我查一下 OTA 升级路线" }
]
```

若 DB 末条 user 与请求末条 content 相同，则不重复追加（防重提）。

### 5.2 编排主链路

```
run_agent_orchestrator
  └─ run_react_loop
        ├─ select_skills
        ├─ create-jira? → run_create_jira_workflow
        └─ else → ReAct tool calling
              ├─ system prompt + Skill body
              ├─ 历史 = 摘要前缀 + 滑动窗口 + 当前 user
              ├─ complete_with_tools
              ├─ 执行工具 / action_required 暂停
              └─ finish → 流式正文
```

**送入推理模型的 messages example（ReAct 一步）**

```json
[
  {
    "role": "system",
    "content": "你是云鲸售后助手…\n## Skill: knowledge\n\n产品故障、排查… → search_knowledge…"
  },
  {
    "role": "user",
    "content": "【会话摘要】\n用户此前询问过 J6 吸力…"
  },
  {
    "role": "assistant",
    "content": "好的，我已了解上述会话背景，将继续在此基础上协助你。"
  },
  { "role": "user", "content": "查一下这台机器信息" },
  { "role": "assistant", "content": "该设备型号为 J6…" },
  { "role": "user", "content": "J6 基站溢水怎么办？SN：YXCAAM2629XB05N0097" }
]
```

同时附带 OpenAI tools（仅选中 Skill 的子集），例如 knowledge：

```json
[
  {
    "type": "function",
    "function": {
      "name": "search_knowledge",
      "description": "检索售后知识库…",
      "parameters": {
        "type": "object",
        "properties": {
          "query": { "type": "string", "description": "检索关键词或问题描述…" }
        },
        "required": ["query"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "get_error_code_info",
      "description": "…",
      "parameters": { "…" : "…" }
    }
  }
]
```

模型若返回 tool_calls，后端执行后追加：

```json
{
  "role": "assistant",
  "content": "",
  "tool_calls": [
    {
      "id": "call_abc123",
      "type": "function",
      "function": {
        "name": "search_knowledge",
        "arguments": "{\"query\":\"J6 基站溢水\"}"
      }
    }
  ]
}
```

```json
{
  "role": "tool",
  "tool_call_id": "call_abc123",
  "content": "{\"summary\":\"…\",\"hits\":[…]}"
}
```

然后进入下一步推理，直到模型输出最终 `content`（无 tool_calls）。

### 5.3 Skill 路由

优先级：

1. `context.intent == "create_jira"`（废弃兼容）
2. `context.skill_ids` 强制指定
3. 关键词匹配（含最近若干轮 user）
4. LLM `classify_intent` → 映射 Skill
5. 失败回退全量 Skill（除 create-jira）

**强制指定 example**

```json
{
  "messages": [{ "role": "user", "content": "对比 J5 和 J6 吸力" }],
  "context": { "skill_ids": ["product-attrs"] }
}
```

**路由结果事件 data example**

```json
{
  "skill_ids": ["knowledge"],
  "direct_reply": false,
  "confidence": 0.85,
  "source": "keyword",
  "elapsed_ms": 0
}
```

或意图分类：

```json
{
  "skill_ids": ["product-attrs", "knowledge"],
  "direct_reply": false,
  "confidence": 0.92,
  "source": "intent:model",
  "elapsed_ms": 380
}
```

| Skill                           | 典型能力                     |
| ------------------------------- | ---------------------------- |
| knowledge                       | 售后知识库 / 错误码 / 语音码 |
| product-attrs                   | 产品参数查询/对比            |
| device-query                    | 设备信息                     |
| sn-info                         | 三码/关联 SN                 |
| dlog                            | 设备日志                     |
| bag-upload                      | BAG 上传开关                 |
| firmware-market / firmware-push | 固件市场与推送               |
| app-whitelist                   | APPID 加白                   |
| network-diagnosis / nw-config   | 网络诊断、配网历史           |
| create-jira                     | 创建 Jira（专用 workflow）   |

### 5.4 ReAct 参数与取消

- 默认 `max_steps=6`（1–12）
- 工具 context 含 `user_question`、`history_messages`、身份、locale、`_generation_id`
- 若已 cancel：工具返回

```json
{
  "cancelled": true,
  "message": "用户已取消该工具执行"
}
```

- 无正文时可能走 `stream_final_answer`，用对话模型根据 steps 再生成回答。

---

## 6. 人机确认（含完整 example）

需确认的动作：

| action_type           | kind         | 说明          |
| --------------------- | ------------ | ------------- |
| `create_jira`       | `workflow` | 建 Jira       |
| `push_firmware`     | `react`    | 推送固件      |
| `switch_bag_upload` | `react`    | 切换 BAG 上传 |
| `add_users_to_app`  | `react`    | APPID 加白    |

### 6.1 SSE `action_required` 控制帧

```json
{
  "id": "msg-aaaa-bbbb-cccc-dddd",
  "object": "chat.completion.chunk",
  "created": 1720591400,
  "model": "agent",
  "choices": [
    {
      "index": 0,
      "delta": {
        "metadata": {
          "type": "action_required",
          "action_id": "act-5555-6666-7777-8888",
          "action_type": "create_jira",
          "title": "创建 Jira 工单",
          "draft": {
            "product_type": "J6",
            "description": "基站溢水，重启无效",
            "sn": "YXCAAM2629XB05N0097",
            "did": null,
            "firmware_version": "1.2.3",
            "region": "cn",
            "issue_time": "2026-06-04 03:01:11",
            "user_email": "zhangsan@narwal.com",
            "resources": [
              "https://support-fae.obs.cn-south-1.myhuaweicloud.com/agent/xxx/overflow.jpg"
            ],
            "summary": "J6 基站溢水"
          },
          "kind": "workflow",
          "respond_api": {
            "method": "POST",
            "path": "/api/chat/actions/respond",
            "description": "用户确认或取消后调用；approved=true 时可将修改后的 draft 放入 draft 字段"
          },
          "status": "paused",
          "confirm_timeout_sec": 300,
          "paused_at": 1720591400,
          "expires_at": 1720591700
        }
      },
      "finish_reason": null
    }
  ]
}
```

此前通常已流式输出 `preview_text` 到 `content`；随后仍有 metadata / stop / `[DONE]`。此时 generation `status=paused`。

### 6.2 对应的 `action_card` block

```json
{
  "type": "action_card",
  "action_id": "act-5555-6666-7777-8888",
  "action_type": "create_jira",
  "title": "创建 Jira 工单",
  "draft": { "product_type": "J6", "description": "基站溢水，重启无效", "sn": "YXCAAM2629XB05N0097" },
  "kind": "workflow",
  "status": "pending",
  "confirm_timeout_sec": 300,
  "paused_at": 1720591400,
  "expires_at": 1720591700,
  "respond_api": {
    "method": "POST",
    "path": "/api/chat/actions/respond",
    "description": "…"
  }
}
```

确认成功后 `status` → `confirmed`，并可能带 `result_text`：

```json
{
  "status": "confirmed",
  "result_text": "Jira 工单已创建成功。\n- 工单号：FAE-12345\n- 链接：https://jira.example.com/browse/FAE-12345"
}
```

取消/超时：`cancelled` / `expired`。

### 6.3 Redis `checkpoint` example（ReAct 工具确认）

```json
{
  "action_id": "act-9999",
  "action_type": "push_firmware",
  "title": "推送固件版本",
  "draft": {
    "sn": "YXCAAM2629XB05N0097",
    "version": "4.5.6"
  },
  "kind": "react",
  "preview_text": "即将执行工具「push_firmware」，参数如下：\n{\n  \"sn\": \"YXCAAM2629XB05N0097\",\n  \"version\": \"4.5.6\"\n}\n\n请确认后执行，或取消操作。",
  "mode": "react",
  "intent": null,
  "steps": [],
  "react_messages": [
    { "role": "system", "content": "…" },
    { "role": "user", "content": "帮这台机器推送固件 4.5.6" },
    {
      "role": "assistant",
      "content": "",
      "tool_calls": [
        {
          "id": "call_push1",
          "type": "function",
          "function": {
            "name": "push_firmware",
            "arguments": "{\"sn\":\"YXCAAM2629XB05N0097\",\"version\":\"4.5.6\"}"
          }
        }
      ]
    }
  ],
  "pending_tool_call": {
    "id": "call_push1",
    "name": "push_firmware",
    "arguments": "{\"sn\":\"YXCAAM2629XB05N0097\",\"version\":\"4.5.6\"}"
  },
  "remaining_tool_calls": [
    {
      "id": "call_push1",
      "name": "push_firmware",
      "arguments": "{\"sn\":\"YXCAAM2629XB05N0097\",\"version\":\"4.5.6\"}"
    }
  ],
  "step_idx": 1,
  "selected_skill_ids": ["firmware-push"],
  "direct_reply": false,
  "confirm_timeout_sec": 300,
  "paused_at": 1720591800,
  "expires_at": 1720592100
}
```

用户确认后，checkpoint 会追加：

```json
{
  "user_response": {
    "approved": true,
    "draft": {
      "sn": "YXCAAM2629XB05N0097",
      "version": "4.5.6"
    }
  }
}
```

### 6.4 确认 / 取消 API

**确认（可改 draft）**

```http
POST /api/chat/actions/respond
```

```json
{
  "generation_id": "gen-1111-2222-3333-4444",
  "action_id": "act-5555-6666-7777-8888",
  "approved": true,
  "draft": {
    "product_type": "J6",
    "description": "基站溢水，重启无效（用户补充：夜间发生）",
    "sn": "YXCAAM2629XB05N0097",
    "region": "cn"
  }
}
```

**响应**

```json
{
  "code": 0,
  "message": "已提交用户响应",
  "data": {
    "generation_id": "gen-1111-2222-3333-4444",
    "message_id": "msg-aaaa-bbbb-cccc-dddd",
    "offset": 28,
    "status": "running",
    "approved": true,
    "celery_task_id": "…",
    "stream_path": "/api/chat/generations/gen-1111-2222-3333-4444/stream"
  }
}
```

前端用返回的 `offset` 续订 SSE，收取建单结果或后续 ReAct 输出。

**取消**

```json
{
  "generation_id": "gen-1111-2222-3333-4444",
  "action_id": "act-5555-6666-7777-8888",
  "approved": false
}
```

超时未确认：Taskiq `chat.expire_generation_action` 自动按取消续跑。

### 6.5 Jira workflow 简要数据流

```
用户：「帮我提单：基站溢水 YXCAAM… 2026-06-04 03:01:11」
  → Skill create-jira
  → LLM 抽字段 + 查三码/设备信息
  → build_jira_draft
  → action_required (kind=workflow)
  → 用户确认
  → execute_jira_create_from_draft
  → 流式输出「工单号 / 链接」
  → workflow_data.status = "created"
```

---

## 7. 停止生成

```http
POST /api/chat/generations/gen-1111-2222-3333-4444/cancel
```

**响应 example**

```json
{
  "code": 0,
  "message": "已停止生成",
  "data": {
    "generation_id": "gen-1111-2222-3333-4444",
    "message_id": "msg-aaaa-bbbb-cccc-dddd",
    "offset": 9,
    "status": "cancelled",
    "already_finished": false,
    "stream_path": "/api/chat/generations/gen-1111-2222-3333-4444/stream"
  }
}
```

- `running`：协作停止，保留已输出；
- `paused`：撤销待确认；
- 无有效输出时 content 变为「用户已停止生成」。

---

## 8. 多轮上下文与长期记忆

### 8.1 送入 LLM 的结构

```
[可选] user: 【会话摘要】\n{summary}
[可选] assistant: 好的，我已了解上述会话背景，将继续在此基础上协助你。
[最近 ≤12 条] user/assistant 原文
[当前] user: 本轮问题
```

常量：

- `SLIDING_WINDOW_MESSAGES = 12`
- `SUMMARIZE_TRIGGER_MESSAGES = 12`
- 单条截断约 8000 字；摘要上限约 3000 字

### 8.2 摘要任务

generation `completed` → `chat.update_session_memory`：

- 将窗口外、未摘要消息增量折叠进 `memory.summary`；
- 更新 `summarized_until_index`。

下一轮创建 generation 时 `load_session_memory` → 注入 `context._session_memory`。

---

## 9. 工具能力一览

| 工具                                                                                             | 说明                      |
| ------------------------------------------------------------------------------------------------ | ------------------------- |
| `search_knowledge`                                                                             | SOP 知识库向量检索 + 总结 |
| `list_product_catalog` / `list_attr_fields` / `get_product_attrs` / `scan_product_attrs` | 产品参数                  |
| `get_robot_info`                                                                               | 设备信息                  |
| `get_sn_info` / `_j1` / `_j2`                                                              | 三码                      |
| `get_dlog_data` / `get_app_logs`                                                             | 设备/APP 日志             |
| `get_error_code_info` / `get_voice_code_info`                                                | 错误码 / 语音码           |
| `get_bag_upload_status` / `switch_bag_upload`※                                              | BAG 上传                  |
| `get_ota_router` / `get_firmware_infos` / `push_firmware`※                                | 固件                      |
| `add_users_to_app`※                                                                           | APPID 加白                |
| `device_network_diagnosis` / `get_nw_config_history`                                         | 网络                      |
| `get_current_time`                                                                             | 当前时间                  |

※ 需用户确认。

**工具 observation 示意（search_knowledge）**

```json
{
  "summary": "基站溢水常见原因包括清水箱密封不良…",
  "hits": [
    {
      "doc_id": "sop-123",
      "title": "基站溢水排查",
      "score": 0.91,
      "content": "步骤1… ![图](https://…) <video…>"
    }
  ],
  "filters": { "region": "cn", "product": "J6" }
}
```

附件：`files[].url` 与 `context.resources` 合并后，建 Jira 时作为 `resources`。

---

## 10. 推荐前端时序（带会话）

```
1. POST /sessions
2. POST /agent  { session_id, stream:true, thinking?, messages:[user], files? }
      → generation_id / stream_path
3. GET  stream?from_offset=0
      → 累加 offset，直到 [DONE]；若 action_required 则 UI 展示确认卡
4. 若需确认：
      POST /actions/respond
      GET  stream?from_offset=<当前 offset>
5. 可选 POST /suggested-questions、/messages/feedback
6. 刷新恢复：
      GET /generations/{id}
      GET /generations/{id}/stream?from_offset=N
7. 停止：POST /generations/{id}/cancel
```

---

## 11. Taskiq 任务一览

| task_name                         | 触发时机                |
| --------------------------------- | ----------------------- |
| `chat.run_generation`           | 创建 generation 后      |
| `chat.continue_generation`      | 用户确认/取消后         |
| `chat.expire_generation_action` | 确认超时                |
| `chat.update_session_memory`    | generation completed 后 |

---

## 12. 模块文件索引

| 路径                                         | 作用                      |
| -------------------------------------------- | ------------------------- |
| `router.py`                                | HTTP API                  |
| `schemas.py`                               | 请求/响应模型             |
| `agent.py`                                 | SSE/JSON 适配、确认后续跑 |
| `orchestrator.py`                          | → ReAct                  |
| `react.py`                                 | tool calling 循环         |
| `skill_router.py` / `skills/`            | Skill 发现与路由          |
| `generation_*.py` / `redis_*.py`         | 后台生成与 Redis          |
| `session_service.py`                       | 会话与反馈                |
| `memory_*.py`                              | 长期记忆                  |
| `message_blocks.py`                        | 有序 blocks               |
| `action_confirm.py`                        | 人机确认                  |
| `jira/`                                    | 建单 workflow             |
| `product_attrs/`                           | 参数查询                  |
| `knowledge_store.py` / `sop_vector/`     | 知识库                    |
| `stream_format.py`                         | SSE 帧格式                |
| `../async_task_module/tasks/chat_tasks.py` | Taskiq 任务               |

---

## 13. 与「售后工单 AI 分析」的区别

`async_task_module/tasks/agent_tasks.py` 的 `agent.chat` 调用外部 **Navo Assist**，写入 `AgentConversationRecordModel`，属于工单侧异步分析，**不是** `/api/chat` FAE Agent 对话链路。

---

## 14. 设计要点小结

1. **会话在 MySQL，流式中间态在 Redis**，支持断线续传。
2. **Skill 路由缩小工具面**；建 Jira 走专用 workflow。
3. **危险写操作必须人机确认**，checkpoint 可恢复 tool_calls。
4. **长期记忆 = 摘要 + 滑动窗口**。
5. **assistant 用 blocks 有序表达** reasoning / text / action_card，并兼容扁平 `content`。
