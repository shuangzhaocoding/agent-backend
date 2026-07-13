# -*- coding: utf-8 -*-
#
# Agent 工具元数据注册：OpenAI tools 定义、是否需用户确认等
#
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    required: list[str] | None = None
    requires_user_confirm: bool = False
    confirm_title: str | None = None

    def to_spec_dict(self) -> dict[str, Any]:
        spec: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
        if self.required is not None:
            spec["required"] = self.required
        return spec

    def to_openai_tool(self) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        required = self.required
        if required is None:
            required = list(self.parameters.keys())
        for param_name, param_def in self.parameters.items():
            if isinstance(param_def, dict):
                properties[param_name] = param_def
            else:
                properties[param_name] = {
                    "type": "string",
                    "description": param_def,
                }
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }


_TOOL_CONFIRM_META: dict[str, dict[str, Any]] = {
    "push_firmware": {
        "requires_user_confirm": True,
        "confirm_title": "推送固件版本",
    },
    "switch_bag_upload": {
        "requires_user_confirm": True,
        "confirm_title": "切换 BAG 日志上传",
    },
    "add_users_to_app": {
        "requires_user_confirm": True,
        "confirm_title": "APPID 加白",
    },
}


class ToolRegistry:
    _tools: dict[str, ToolDefinition] = {}

    @classmethod
    def register(cls, tool: ToolDefinition) -> ToolDefinition:
        cls._tools[tool.name] = tool
        return tool

    @classmethod
    def get(cls, name: str) -> ToolDefinition | None:
        return cls._tools.get(name)

    @classmethod
    def all(cls) -> list[ToolDefinition]:
        return list(cls._tools.values())

    @classmethod
    def names(cls) -> frozenset[str]:
        return frozenset(cls._tools.keys())

    @classmethod
    def confirm_required_names(cls) -> frozenset[str]:
        return frozenset(
            name for name, tool in cls._tools.items() if tool.requires_user_confirm
        )

    @classmethod
    def action_titles(cls) -> dict[str, str]:
        return {
            tool.name: tool.confirm_title
            for tool in cls._tools.values()
            if tool.confirm_title
        }

    @classmethod
    def to_specs(cls, tool_names: frozenset[str] | None = None) -> list[dict[str, Any]]:
        tools = cls.all()
        if tool_names is not None:
            tools = [tool for tool in tools if tool.name in tool_names]
        return [tool.to_spec_dict() for tool in tools]

    @classmethod
    def to_openai_tools(cls, tool_names: frozenset[str] | None = None) -> list[dict[str, Any]]:
        tools = cls.all()
        if tool_names is not None:
            tools = [tool for tool in tools if tool.name in tool_names]
        return [tool.to_openai_tool() for tool in tools]


def register_from_specs(specs: list[dict[str, Any]]) -> None:
    """从 legacy TOOL_SPECS 批量注册到 ToolRegistry。"""
    for spec in specs:
        meta = _TOOL_CONFIRM_META.get(spec["name"], {})
        ToolRegistry.register(
            ToolDefinition(
                name=spec["name"],
                description=spec["description"],
                parameters=spec["parameters"],
                required=spec.get("required"),
                requires_user_confirm=bool(meta.get("requires_user_confirm")),
                confirm_title=meta.get("confirm_title"),
            )
        )
