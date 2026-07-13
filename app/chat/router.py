# -*- coding: utf-8 -*-
#
# Agent 对话 API（ReAct + messages 格式 + 可选流式）
#
from typing import Union

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse

from chat.locale import apply_locale_to_context, resolve_request_locale
from chat.agent import run_agent_complete, run_agent_stream
from chat.generation_service import (
    cancel_generation,
    create_generation_for_session,
    get_generation,
    respond_to_generation_action,
    schedule_generation_task,
    stream_generation_chunks,
)
from chat.context_usage_service import (
    compute_context_usage_for_payload,
    compute_context_usage_for_session,
)
from chat.schemas import (
    AgentChatRequest,
    GenerationActionRespondRequest,
    CreateChatSessionRequest,
    MessageFeedbackRequest,
    SuggestedQuestionsRequest,
    UpdateChatSessionTitleRequest,
)
from chat.session_service import (
    create_chat_session,
    delete_chat_session,
    get_chat_session,
    list_chat_sessions,
    submit_message_feedback,
    update_chat_session_title,
)
from chat.suggested_questions import build_suggested_questions_response
from common.auth_token import verify_token
from schema import ResponseFailed, ResponseSuccess

router = APIRouter(
    dependencies=[Depends(verify_token)]
)


def _inject_request_locale(
    payload: AgentChatRequest,
    accept_language: str | None,
    language: str | None,
) -> None:
    locale = resolve_request_locale(accept_language, language)
    payload.context = apply_locale_to_context(payload.context, locale)


def _apply_agent_user_context(
    payload: AgentChatRequest,
    token_user: dict,
) -> None:
    context = dict(payload.context or {})
    user_email = token_user.get("email") or token_user.get("username") or ""
    username = token_user.get("username") or ""
    if user_email:
        context.setdefault("user_email", user_email)
    if username:
        context.setdefault("username", username)
    payload.context = context


_AGENT_STREAM_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


async def _generation_sse_iterator(
    generation_id: str,
    username: str,
    from_offset: int = 0,
):
    async for line in stream_generation_chunks(
        generation_id,
        username,
        from_offset=from_offset,
    ):
        yield line


@router.post("/agent", summary="Agent 对话（意图路由 + workflow / ReAct + 流式/非流式）")
async def agent_chat(
    payload: AgentChatRequest,
    token_user: dict = Depends(verify_token),
    accept_language: Union[str, None] = Header(None, alias="Accept-Language"),
    language: Union[str, None] = Header(None, convert_underscores=True),
):
    """
    请求体示例：
    ```json
    {
        "messages": [{"role": "user", "content": "你好"}],
        "stream": true,
        "files": [
            {
                "name": "screenshot.jpg",
                "url": "https://support-fae.obs.cn-south-1.myhuaweicloud.com/agent/.../screenshot.jpg",
                "size": 3764164,
                "type": "image/jpeg"
            }
        ]
    }
    ```

    - messages：最后一条须为 user；传 session_id 时仅需本轮 user，历史由后端从 DB 拼接
    - files：当前轮上传文件（图片/视频/文档 URL），建 Jira 时自动作为附件
    - stream=true：OpenAI chat.completion.chunk 流式（data: {...} / data: [DONE]）
      流式结束前会额外输出一条 delta.metadata（含 mode / intent / workflow_data / steps）
    - thinking=true：delta 中输出 reasoning_content（思考过程）
    - stream=false：一次性 JSON 返回（data 中含 workflow_data）
    - session_id + stream=true：立即返回 generation_id，前端用 GET /generations/{id}/stream?from_offset= 拉流
    - resume_generation_id：等价于 GET /generations/{id}/stream（兼容旧客户端）
    - context.skill_ids：强制指定 Skill 列表（如 ["product-attrs"]），跳过 Skill 路由
    - context.intent：（已废弃）create_jira 会映射为对应 Skill
    - context.locale：可选；未传时从请求头 Accept-Language 或 language 解析
    """
    _inject_request_locale(payload, accept_language, language)
    _apply_agent_user_context(payload, token_user)
    username = token_user.get("username") or ""

    if payload.stream and payload.resume_generation_id:
        from_offset = payload.resume_from_offset or 0
        return StreamingResponse(
            _generation_sse_iterator(
                payload.resume_generation_id,
                username,
                from_offset=from_offset,
            ),
            media_type="text/event-stream",
            headers=_AGENT_STREAM_HEADERS,
        )


    if payload.stream and payload.session_id:
        try:
            info = await create_generation_for_session(
                payload.session_id,
                username,
                payload,
            )
            celery_task_id = None
            if not info.get("reused"):
                celery_task_id = await schedule_generation_task(info["generation_id"])
                if celery_task_id:
                    from chat.redis_generation import update_generation_meta

                    await update_generation_meta(
                        info["generation_id"],
                        {"celery_task_id": celery_task_id},
                    )
            data = {
                "generation_id": info["generation_id"],
                "message_id": info["message_id"],
                "chunk_id": info["chunk_id"],
                "offset": 0,
                "status": "running",
                "reused": bool(info.get("reused")),
                "celery_task_id": celery_task_id,
                "stream_path": f"/api/chat/generations/{info['generation_id']}/stream",
                "context_usage": info.get("context_usage"),
                "question_at": info.get("question_at"),
            }
            return ResponseSuccess(data=data, message="生成任务已创建")
        except ValueError as exc:
            return ResponseFailed(message=str(exc))

    if payload.stream:
        return StreamingResponse(
            run_agent_stream(payload),
            media_type="text/event-stream",
            headers=_AGENT_STREAM_HEADERS,
        )

    try:
        data = await run_agent_complete(payload)
        return ResponseSuccess(data=data, message="对话成功")
    except Exception as exc:
        return ResponseFailed(message=str(exc))


@router.post("/agent-debug", summary="Agent 调试对话（进程内直连，不走 Taskiq）")
async def agent_chat_debug(
    payload: AgentChatRequest,
    token_user: dict = Depends(verify_token),
    accept_language: Union[str, None] = Header(None, alias="Accept-Language"),
    language: Union[str, None] = Header(None, convert_underscores=True),
):
    """
    与 /agent 请求体相同，但始终在 API 进程内直接执行，不创建 generation、不投递 Taskiq。

    - 忽略 `session_id` 的后台生成逻辑（即使传入也同步跑完）
    - 忽略 `resume_generation_id`
    - `stream=true`：直接 SSE 流式输出
    - `stream=false`：一次性 JSON 返回

    适用于本地/联调调试 ReAct、工具调用与知识库检索。
    """
    _inject_request_locale(payload, accept_language, language)
    _apply_agent_user_context(payload, token_user)

    if payload.stream:
        return StreamingResponse(
            run_agent_stream(payload),
            media_type="text/event-stream",
            headers=_AGENT_STREAM_HEADERS,
        )

    try:
        data = await run_agent_complete(payload)
        return ResponseSuccess(data=data, message="对话成功")
    except Exception as exc:
        return ResponseFailed(message=str(exc))


@router.post("/context-usage", summary="预估上下文 token 用量（分类）")
async def estimate_context_usage(
    payload: AgentChatRequest,
    mode: str = Query(
        "react",
        description="react=含工具定义",
    ),
    token_user: dict = Depends(verify_token),
    accept_language: Union[str, None] = Header(None, alias="Accept-Language"),
    language: Union[str, None] = Header(None, convert_underscores=True),
):
    """
    发送前预估本轮送入模型的上下文 token 用量，按类别拆分。

    - 传 session_id + 本轮 user 消息：与 /agent 相同，历史从 DB 拼接
    - 会话初始化预览：可传 `[{"role":"user","content":""}]`，仅统计已有历史，不追加空消息
    - 不传 session_id：仅基于 messages 预估

    响应 data.categories：system_prompt / memory_summary / recent_messages /
    current_user / tools_schema 等；total_estimated_input 相对 context_limit=1M。
    """
    _inject_request_locale(payload, accept_language, language)
    username = token_user.get("username") or token_user.get("email") or ""
    try:
        if payload.session_id:
            data = await compute_context_usage_for_session(
                payload.session_id,
                username,
                payload,
                mode=mode,
            )
        else:
            data = await compute_context_usage_for_payload(payload, mode=mode)
        return ResponseSuccess(data=data, message="预估成功")
    except ValueError as exc:
        return ResponseFailed(message=str(exc))
    except Exception as exc:
        return ResponseFailed(message=str(exc))


@router.get("/generations/{generation_id}", summary="查询生成任务状态（断线续传）")
async def get_generation_status(
    generation_id: str,
    token_user: dict = Depends(verify_token),
):
    """
    返回生成任务当前状态、已输出 content、chunk offset 等。
    刷新页面后可先 GET 本接口恢复展示，再通过 stream 续传。
    """
    username = token_user.get("username") or token_user.get("email") or ""
    data = await get_generation(generation_id, username)
    if not data:
        return ResponseFailed(message="生成任务不存在或无权访问")
    return ResponseSuccess(data=data, message="获取成功")


@router.post("/generations/{generation_id}/cancel", summary="停止正在进行的生成任务")
async def cancel_generation_task(
    generation_id: str,
    token_user: dict = Depends(verify_token),
):
    """
    用户点击「停止生成」时调用。保留已输出内容，将任务标记为 `cancelled`。

    - `running`：通知 Worker 协作停止，SSE 流会收到 `stop` + `[DONE]`（若尚未结束）
    - `paused`：撤销待确认状态，不再等待用户确认
    - 已完成/已失败/已取消：幂等返回当前状态

    前端可在调用后保持 SSE 连接直至 `[DONE]`，或关闭连接并更新 UI 状态。
    """
    username = token_user.get("username") or token_user.get("email") or ""
    try:
        data = await cancel_generation(generation_id, username)
        return ResponseSuccess(data=data, message="已停止生成")
    except ValueError as exc:
        return ResponseFailed(message=str(exc))
    except Exception as exc:
        return ResponseFailed(message=str(exc))


@router.get("/generations/{generation_id}/stream", summary="续传生成任务 SSE 流")
async def stream_generation(
    generation_id: str,
    from_offset: int = Query(0, ge=0, description="chunk 偏移，从该下标继续推送"),
    token_user: dict = Depends(verify_token),
):
    """
    从 `from_offset` 续传同一次生成的 SSE chunks（真续传）。
    生成在后台继续，断线后刷新页面可重新订阅。
    """
    username = token_user.get("username") or token_user.get("email") or ""
    if not await get_generation(generation_id, username):
        return ResponseFailed(message="生成任务不存在或无权访问")

    return StreamingResponse(
        _generation_sse_iterator(generation_id, username, from_offset=from_offset),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/actions/respond", summary="用户确认或取消待执行动作，并续跑生成任务")
async def respond_generation_action(
    payload: GenerationActionRespondRequest,
    token_user: dict = Depends(verify_token),
):
    """
    在流式输出出现 `action_required` 且任务 `status=paused` 后调用。

    **请求示例（确认创建 Jira）**
    ```json
    {
        "generation_id": "...",
        "action_id": "...",
        "approved": true,
        "draft": {
            "product_type": "J6",
            "description": "基站溢水",
            "sn": "YXCAAM2629XB05N0097"
        }
    }
    ```

    **取消**
    ```json
    {
        "generation_id": "...",
        "action_id": "...",
        "approved": false
    }
    ```

    成功后请用返回的 `stream_path` + 当前 `offset` 续订 SSE 收后续输出。
    """
    username = token_user.get("username") or token_user.get("email") or ""
    try:
        data = await respond_to_generation_action(
            payload.generation_id,
            username,
            action_id=payload.action_id,
            approved=payload.approved,
            draft=payload.draft,
        )
        return ResponseSuccess(data=data, message="已提交用户响应")
    except ValueError as exc:
        return ResponseFailed(message=str(exc))
    except Exception as exc:
        return ResponseFailed(message=str(exc))


@router.post("/suggested-questions", summary="生成「你可能还想问」推荐追问")
async def suggested_questions(payload: SuggestedQuestionsRequest):
    """
    在 Agent 对话结束后由前端单独调用，根据对话历史生成推荐追问。

    请求体示例（messages 末条为 assistant）：
    ```json
    {
        "messages": [
            {"role": "user", "content": "J6 吸力多少"},
            {"role": "assistant", "content": "J6 最大吸力为 12000Pa..."}
        ],
        "count": 3
    }
    ```

    或传入 assistant_answer（messages 末条仍为 user 时）：
    ```json
    {
        "messages": [{"role": "user", "content": "J6 吸力多少"}],
        "assistant_answer": "J6 最大吸力为 12000Pa...",
        "intent": "query_product_attrs",
        "mode": "workflow_query_product_attrs"
    }
    ```

    响应 data：
    - title：固定为「你可能还想问」
    - questions / suggested_questions：推荐问题列表
    """
    try:
        data = await build_suggested_questions_response(payload)
        return ResponseSuccess(data=data, message="生成成功")
    except Exception as exc:
        return ResponseFailed(message=str(exc))


@router.get("/sessions", summary="分页获取当前用户的 Agent 会话列表")
async def list_sessions(
    page: int = Query(1, ge=1, description="页码"),
    per_page: int = Query(20, ge=1, le=100, description="每页数量"),
    token_user: dict = Depends(verify_token),
):
    """
    按 modified_at 倒序返回会话摘要，不含 messages 正文（详情见 GET /sessions/{session_id}）。

    响应 data：
    - total / page / per_page
    - items: [{ session_id, title, modified_at, message_count }]
    """
    username = token_user.get("username") or token_user.get("email") or ""
    try:
        data = await list_chat_sessions(username, page=page, per_page=per_page)
        return ResponseSuccess(data=data, message="获取成功")
    except ValueError as exc:
        return ResponseFailed(message=str(exc))
    except Exception as exc:
        return ResponseFailed(message=str(exc))


@router.post("/sessions", summary="创建 Agent 会话并落库")
async def create_session(
    payload: CreateChatSessionRequest,
    token_user: dict = Depends(verify_token),
):
    """
    创建新会话，返回 session_id（可由前端传入或后端生成 UUID）。
    messages 初始为 []，后续由后端在对话生成过程中写入。
    """
    username = token_user.get("username") or token_user.get("email") or ""
    try:
        data = await create_chat_session(
            username,
            title=payload.title,
            session_id=payload.session_id,
        )
        return ResponseSuccess(data=data, message="会话创建成功")
    except ValueError as exc:
        return ResponseFailed(message=str(exc))
    except Exception as exc:
        return ResponseFailed(message=str(exc))


@router.get("/sessions/{session_id}", summary="获取 Agent 会话详情")
async def get_session(
    session_id: str,
    token_user: dict = Depends(verify_token),
):
    username = token_user.get("username") or token_user.get("email") or ""
    try:
        data = await get_chat_session(session_id, username)
        return ResponseSuccess(data=data, message="获取成功")
    except ValueError as exc:
        return ResponseFailed(message=str(exc))
    except Exception as exc:
        return ResponseFailed(message=str(exc))


@router.delete("/sessions/{session_id}", summary="删除 Agent 会话")
async def delete_session(
    session_id: str,
    token_user: dict = Depends(verify_token),
):
    """
    软删除会话：将 status 置为 0（已删除），保留消息与评价数据。
    若存在进行中的生成任务，会先停止生成并清理 Redis 状态。
    """
    username = token_user.get("username") or token_user.get("email") or ""
    try:
        data = await delete_chat_session(session_id, username)
        return ResponseSuccess(data=data, message="会话删除成功")
    except ValueError as exc:
        return ResponseFailed(message=str(exc))
    except Exception as exc:
        return ResponseFailed(message=str(exc))


@router.patch("/sessions/{session_id}/title", summary="修改 Agent 会话标题")
async def update_session_title(
    session_id: str,
    payload: UpdateChatSessionTitleRequest,
    token_user: dict = Depends(verify_token),
):
    """
    仅更新会话标题，不影响 messages。

    请求体示例：
    ```json
    { "title": "J6 基站溢水排查" }
    ```
    """
    username = token_user.get("username") or token_user.get("email") or ""
    try:
        data = await update_chat_session_title(session_id, username, payload.title)
        return ResponseSuccess(data=data, message="标题更新成功")
    except ValueError as exc:
        return ResponseFailed(message=str(exc))
    except Exception as exc:
        return ResponseFailed(message=str(exc))


@router.post("/messages/feedback", summary="对单条 assistant 消息点赞/点踩/取消")
async def message_feedback(
    payload: MessageFeedbackRequest,
    token_user: dict = Depends(verify_token),
):
    """
    对会话内单条 assistant 消息评价。

    - vote=1 点赞
    - vote=-1 点踩（category 必填，见 FEEDBACK_CATEGORIES）
    - vote=0 取消评价

    message_id 对应 assistant 消息 metadata.id（流式 chunk id / UUID）。
    """
    username = token_user.get("username") or token_user.get("email") or ""
    try:
        data = await submit_message_feedback(
            session_id=payload.session_id,
            message_id=payload.message_id,
            vote=payload.vote,
            creator=username,
            category=payload.category,
            comment=payload.comment,
        )
        return ResponseSuccess(data=data, message="评价提交成功")
    except ValueError as exc:
        return ResponseFailed(message=str(exc))
    except Exception as exc:
        return ResponseFailed(message=str(exc))

