# -*- coding: utf-8 -*-
import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"] = Field(..., description="消息角色")
    content: str = Field(..., description="消息内容")
    metadata: Optional[dict[str, Any]] = Field(
        None,
        description=(
            "可选元数据；assistant 消息可含 blocks（有序 reasoning/text/action_card）"
            "与 workflow_data.draft 供 Jira 确认轮次使用"
        ),
    )


class ChatFileItem(BaseModel):
    name: str = Field(..., description="文件名")
    url: str = Field(..., description="文件 OBS 访问 URL")
    size: Optional[int] = Field(None, description="文件大小（字节）")
    type: Optional[str] = Field(None, description="MIME 类型，如 image/jpeg、video/mp4")


class AgentChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(
        ...,
        min_length=1,
        description=(
            "对话消息列表，最后一条须为 user。"
            "传入 session_id 时仅需提交本轮 user 消息，历史由后端从会话 DB 拼接"
        ),
    )
    stream: bool = Field(True, description="是否流式输出（OpenAI chat.completion.chunk 格式）")
    thinking: bool = Field(
        False,
        description="是否流式输出思考过程（delta.reasoning_content，供 TinyRobot thinkingPlugin）",
    )
    context: Optional[dict[str, Any]] = Field(
        None,
        description=(
            "可选上下文。"
            "skill_ids: 强制 Skill 列表（如 [\"product-attrs\"]），跳过 Skill 路由；"
            "intent: （已废弃）create_jira 会映射为对应 Skill；"
            "sn/did/description/issue_time/firmware_version/app_version/app_id/"
            "issue_index（多条问题时选第几条，从0开始）/product_type/"
            "region（cn|ovs，查产品参数时区分海内外）/products|product（强制指定型号）/"
            "user_email/username（操作人，用于 APPID 加白等）/"
            "locale/language（回复语言，如 zh-CN/en-US；也可由请求头 Accept-Language 或 language 注入）/"
            "jira_draft（可选，上一轮 pending 草稿；通常由 messages.metadata 自动携带）等"
        ),
    )
    temperature: Optional[float] = Field(0.7, description="生成温度", ge=0, le=2)
    max_steps: Optional[int] = Field(
        6,
        description="ReAct 最大推理步数（含工具调用轮次）",
        ge=1,
        le=12,
    )
    files: Optional[list[ChatFileItem]] = Field(
        None,
        description="当前轮用户上传的文件列表（图片/视频/文档等），建 Jira 时作为附件资源",
    )
    session_id: Optional[str] = Field(
        None,
        description="会话 ID；传入后启用后台生成 + 断线续传（消息写入 chat_agent_session）",
    )
    resume_generation_id: Optional[str] = Field(
        None,
        description="续传已有生成任务 ID（刷新后重连 SSE，无需重新生成）",
    )
    resume_from_offset: Optional[int] = Field(
        0,
        description="续传 SSE chunk 偏移（与 generation.chunks 下标对齐）",
        ge=0,
    )

    @field_validator("messages")
    @classmethod
    def validate_messages(cls, messages: list[ChatMessage]) -> list[ChatMessage]:
        chat_messages = [m for m in messages if m.role != "system"]
        if not chat_messages:
            raise ValueError("messages 不能为空")
        if chat_messages[-1].role != "user":
            raise ValueError("messages 最后一条必须是 user 角色")
        return messages

    def get_history_messages(self) -> list[dict[str, str]]:
        """历史对话（不含当前轮 user；有 session 记忆时含摘要前缀 + 滑动窗口）。"""
        chat_messages = [m for m in self.messages if m.role != "system"]
        history = chat_messages[:-1]
        from chat.memory_service import session_memory_from_context
        from chat.memory_context import build_llm_history_messages

        memory = session_memory_from_context(self.context)
        if memory is not None:
            return build_llm_history_messages(history, memory)
        return [{"role": m.role, "content": m.content} for m in history]

    def get_llm_dialog_messages(self) -> list[dict[str, str]]:
        """摘要 + 滑动窗口历史 + 当前 user。"""
        return [
            *self.get_history_messages(),
            {"role": "user", "content": self.get_current_user_content()},
        ]

    def get_current_user_content(self) -> str:
        """当前轮用户消息（最后一条 user）。"""
        chat_messages = [m for m in self.messages if m.role != "system"]
        parts = [chat_messages[-1].content.strip()]
        # if self.context:
        #     parts.append(
        #         f"附加上下文：{json.dumps(self.context, ensure_ascii=False)}"
        #     )
        return "\n".join(parts)

    def get_file_urls(self) -> list[str]:
        """提取 files 中的 URL 列表（去重、去空）。"""
        urls: list[str] = []
        seen: set[str] = set()
        for item in self.files or []:
            url = (item.url or "").strip()
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
        return urls

    def get_attachment_urls(self, context: Optional[dict[str, Any]] = None) -> list[str]:
        """合并 files 与 context.resources 中的附件 URL。"""
        urls = list(self.get_file_urls())
        seen = set(urls)
        context = context or self.context or {}
        raw_resources = context.get("resources")
        if not raw_resources:
            return urls
        for item in raw_resources:
            if isinstance(item, str):
                url = item.strip()
            elif isinstance(item, dict):
                url = str(item.get("url") or "").strip()
            else:
                continue
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
        return urls

    def files_for_llm(self) -> list[dict[str, Any]]:
        """供大模型或日志使用的文件摘要（不含大字段）。"""
        return [
            {
                "name": f.name,
                "url": f.url,
                "size": f.size,
                "type": f.type,
            }
            for f in self.files or []
        ]


class SuggestedQuestionsRequest(BaseModel):
    messages: list[ChatMessage] = Field(
        ...,
        min_length=1,
        description="完整对话消息；末条为 assistant 时视为最新回答，末条为 user 时需传 assistant_answer",
    )
    assistant_answer: Optional[str] = Field(
        None,
        description="助手最新回答正文；若 messages 末条已是 assistant 可省略",
    )
    intent: Optional[str] = Field(None, description="本轮对话意图（可选，辅助生成）")
    mode: Optional[str] = Field(None, description="本轮对话模式（可选，如 react / workflow_create_jira）")
    count: int = Field(3, description="推荐问题数量", ge=1, le=5)

    @field_validator("messages")
    @classmethod
    def validate_suggested_messages(cls, messages: list[ChatMessage]) -> list[ChatMessage]:
        chat_messages = [m for m in messages if m.role != "system"]
        if not chat_messages:
            raise ValueError("messages 不能为空")
        return messages

    def _chat_messages(self) -> list[ChatMessage]:
        return [m for m in self.messages if m.role != "system"]

    def get_assistant_answer(self) -> str:
        explicit = (self.assistant_answer or "").strip()
        if explicit:
            return explicit
        chat_messages = self._chat_messages()
        if chat_messages and chat_messages[-1].role == "assistant":
            return chat_messages[-1].content.strip()
        return ""

    def get_history_messages(self) -> list[dict[str, str]]:
        chat_messages = self._chat_messages()
        if not chat_messages:
            return []
        if chat_messages[-1].role == "assistant":
            history = chat_messages[:-1]
        else:
            history = chat_messages[:-1] if chat_messages[-1].role == "user" else chat_messages
        return [{"role": m.role, "content": m.content} for m in history]

    def get_current_user_content(self) -> str:
        chat_messages = self._chat_messages()
        if not chat_messages:
            return ""
        if chat_messages[-1].role == "user":
            return chat_messages[-1].content.strip()
        for message in reversed(chat_messages):
            if message.role == "user":
                return message.content.strip()
        return ""


class CreateChatSessionRequest(BaseModel):
    title: Optional[str] = Field(None, description="会话标题，可选")
    session_id: Optional[str] = Field(
        None,
        description="会话ID，可选；不传则由后端生成 UUID",
    )


class UpdateChatSessionTitleRequest(BaseModel):
    title: str = Field(..., description="会话标题")


class JiraIssueDraft(BaseModel):
    """Jira 建单草稿，由 workflow pending 阶段返回，确认接口原样回传。"""

    product_type: str = Field(..., min_length=1, description="产品型号（product_full_name）")
    description: str = Field(..., min_length=1, description="问题描述")
    user_email: Optional[str] = Field(None, description="提单人邮箱")
    sn: Optional[str] = Field(None, description="机器人 SN")
    did: Optional[str] = Field(None, description="设备 DID（32位）")
    firmware_version: Optional[str] = Field(None, description="固件版本")
    region: Literal["cn", "ovs"] = Field("cn", description="区域")
    bill_code: Optional[str] = Field(None, description="售后工单号")
    issue_time: Optional[str] = Field(None, description="问题发生时间")
    app_version: Optional[str] = Field(None, description="APP 版本")
    app_id: Optional[str] = Field(None, description="APPID")
    resources: list[str] = Field(default_factory=list, description="附件 URL 列表")
    summary: Optional[str] = Field(None, description="Jira 标题摘要")
    robot_info: Optional[dict[str, Any]] = Field(None, description="设备信息快照")
    issues_count: Optional[int] = Field(None, description="识别到的多条问题数量")
    issue_index: Optional[int] = Field(None, description="当前处理的问题序号（从0开始）")

    @field_validator("sn", "did", "product_type", "description", mode="before")
    @classmethod
    def strip_text_fields(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("resources", mode="before")
    @classmethod
    def normalize_resources(cls, value: Any) -> list[str]:
        if not value:
            return []
        if not isinstance(value, list):
            return []
        urls: list[str] = []
        seen: set[str] = set()
        for item in value:
            url = str(item or "").strip()
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
        return urls

    @model_validator(mode="after")
    def check_sn_or_did(self) -> "JiraIssueDraft":
        if not (self.sn or self.did):
            raise ValueError("draft 中 sn 与 did 至少填写一项")
        return self


class GenerationActionRespondRequest(BaseModel):
    generation_id: str = Field(..., description="生成任务 ID")
    action_id: str = Field(..., description="action_required 控制帧中的 action_id")
    approved: bool = Field(..., description="true=确认执行，false=取消")
    draft: Optional[dict[str, Any]] = Field(
        None,
        description="用户修改后的参数（可选）；Jira 为 draft 结构，工具为 action_input",
    )


class MessageFeedbackRequest(BaseModel):
    session_id: str = Field(..., description="会话ID")
    message_id: str = Field(..., description="assistant 消息 metadata.id")
    vote: int = Field(..., description="1 点赞 / -1 点踩 / 0 取消")
    category: Optional[str] = Field(None, description="点踩类别（vote=-1 时必填）")
    comment: Optional[str] = Field(None, description="点踩说明（vote=-1 时可选）")
