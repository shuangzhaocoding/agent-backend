# -*- coding: utf-8 -*-
from typing import Any

from openai import AsyncOpenAI

from common import config_file

config = config_file.read_conf(config_file.config_dir)
deepseek_config = config.get("deepseek", {})
DEEPSEEK_API_KEY = deepseek_config.get("api_key")
DEEPSEEK_BASE_URL = deepseek_config.get("base_url")
DEEPSEEK_MODEL = deepseek_config.get("model")
DEEPSEEK_MODEL_REASONER = deepseek_config.get("model_reasoner") or DEEPSEEK_MODEL
NON_CASUAL_REASONING_EFFORT = deepseek_config.get("reasoning_effort") or "high"

JSON_RESPONSE_FORMAT: dict[str, str] = {"type": "json_object"}


def get_deepseek_client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)


async def complete_json(
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.1,
    model: str | None = None,
) -> Any:
    """非流式调用，强制 response_format=json_object。"""
    client = get_deepseek_client()
    return await client.chat.completions.create(
        model=model or DEEPSEEK_MODEL,
        messages=messages,
        stream=False,
        temperature=temperature,
        response_format=JSON_RESPONSE_FORMAT,
    )

async def complete_with_tools(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    temperature: float = 0.2,
    model: str | None = None,
    reasoning_effort: str | None = NON_CASUAL_REASONING_EFFORT,
) -> Any:
    """调用推理模型，返回完整 ChatCompletion（可能含 tool_calls）。"""
    client = get_deepseek_client()
    kwargs: dict[str, Any] = {
        "model": model or DEEPSEEK_MODEL_REASONER,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    return await client.chat.completions.create(**kwargs)


async def iter_completion_deltas(
    messages: list[dict[str, str]],
    temperature: float = 0.7,
    model: str | None = None,
    generation_id: str | None = None,
    reasoning_effort: str | None = None,
):
    async for part in iter_completion_stream_parts(
        messages,
        temperature=temperature,
        model=model,
        generation_id=generation_id,
        reasoning_effort=reasoning_effort,
    ):
        if part.get("content"):
            yield part["content"]


async def iter_completion_stream_parts(
    messages: list[dict[str, str]],
    temperature: float = 0.7,
    model: str | None = None,
    generation_id: str | None = None,
    reasoning_effort: str | None = None,
):
    """流式返回 content 与 reasoning_content（若模型支持）。"""
    from chat.generation_cancel import is_generation_cancelled

    client = get_deepseek_client()
    kwargs: dict[str, Any] = {
        "model": model or DEEPSEEK_MODEL,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
    }
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    stream = await client.chat.completions.create(**kwargs)
    chunk_idx = 0
    async for chunk in stream:
        if generation_id and chunk_idx % 8 == 0 and await is_generation_cancelled(generation_id):
            break
        chunk_idx += 1
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        reasoning = getattr(delta, "reasoning_content", None) or ""
        content = delta.content or ""
        if reasoning or content:
            yield {
                "reasoning_content": reasoning,
                "content": content,
            }
