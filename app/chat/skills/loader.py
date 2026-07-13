# -*- coding: utf-8 -*-
#
# Cursor 风格 Skill 目录加载：每个 skill 为子目录 + SKILL.md（YAML frontmatter + Markdown body）
#
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_SKILLS_ROOT = Path(__file__).parent
_SKILLS_CACHE: list[AgentSkill] | None = None
_EXCLUDED_SKILL_IDS = frozenset({"casual", "create-jira"})


@dataclass(frozen=True)
class AgentSkill:
    name: str
    description: str
    tools: tuple[str, ...] = ()
    body: str = ""
    react_hint: str = ""
    casual_capability: str = ""
    keywords: tuple[str, ...] = ()
    workflow: str = ""
    enabled: bool = True
    skill_dir: str = ""

    @property
    def has_tools(self) -> bool:
        return bool(self.tools)

    @property
    def is_workflow_only(self) -> bool:
        return bool(self.workflow) and not self.tools


CREATE_JIRA_SKILL_ID = "create-jira"


def _split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    text = content.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}, text.strip()
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text.strip()
    meta = yaml.safe_load(parts[1]) or {}
    body = parts[2].strip()
    if not isinstance(meta, dict):
        meta = {}
    return meta, body


def _parse_skill_md(skill_dir: Path) -> AgentSkill | None:
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file():
        return None
    meta, body = _split_frontmatter(skill_file.read_text(encoding="utf-8"))
    name = str(meta.get("name") or skill_dir.name).strip()
    description = str(meta.get("description") or "").strip()
    if not name or not description:
        return None
    if name in _EXCLUDED_SKILL_IDS:
        return None
    tools_raw = meta.get("tools") or []
    tools = tuple(str(item).strip() for item in tools_raw if str(item).strip())
    keywords_raw = meta.get("keywords") or []
    keywords = tuple(str(item).strip().lower() for item in keywords_raw if str(item).strip())
    enabled = meta.get("enabled", True)
    if enabled is False:
        return None
    return AgentSkill(
        name=name,
        description=description,
        tools=tools,
        body=body,
        react_hint=str(meta.get("react_hint") or "").strip(),
        casual_capability=str(meta.get("casual_capability") or "").strip(),
        keywords=keywords,
        workflow=str(meta.get("workflow") or "").strip(),
        enabled=True,
        skill_dir=skill_dir.name,
    )


def discover_skills(*, reload: bool = False) -> list[AgentSkill]:
    global _SKILLS_CACHE
    if _SKILLS_CACHE is not None and not reload:
        return _SKILLS_CACHE

    skills: list[AgentSkill] = []
    for child in sorted(_SKILLS_ROOT.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".")):
            continue
        if child.name == "casual":
            continue
        skill = _parse_skill_md(child)
        if skill:
            skills.append(skill)

    _SKILLS_CACHE = skills
    return skills


def get_skill(name: str) -> AgentSkill | None:
    for skill in discover_skills():
        if skill.name == name:
            return skill
    return None


def get_skills(names: list[str]) -> list[AgentSkill]:
    by_name = {skill.name: skill for skill in discover_skills()}
    return [by_name[name] for name in names if name in by_name]


def all_agent_skill_ids() -> list[str]:
    """除 create-jira 外全部 Skill，用于全量加载工具与指令。"""
    return [skill.name for skill in discover_skills() if skill.name != CREATE_JIRA_SKILL_ID]


def resolve_workflow_for_skills(skill_names: list[str]) -> str | None:
    """若选中 workflow 类 Skill，返回 workflow 名称（如 create_jira）。"""
    for skill in get_skills(skill_names):
        if skill.workflow:
            return skill.workflow
    return None


def should_route_create_jira_workflow(skill_names: list[str]) -> bool:
    return CREATE_JIRA_SKILL_ID in skill_names


def resolve_tool_names_for_skills(skill_names: list[str]) -> frozenset[str]:
    names: set[str] = set()
    for skill in get_skills(skill_names):
        names.update(skill.tools)
    return frozenset(names)


def build_skill_catalog_text() -> str:
    """Skill 目录（name + description），供调试或文档使用。"""
    lines = ["可用 Skill 目录（name: description）："]
    for skill in discover_skills():
        lines.append(f"- {skill.name}: {skill.description}")
    return "\n".join(lines)


def build_skill_instructions(skill_names: list[str]) -> str:
    """加载指定 Skill 的完整 Markdown 指令正文。"""
    sections: list[str] = []
    for skill in get_skills(skill_names):
        if not skill.body:
            continue
        sections.append(f"## Skill: {skill.name}\n\n{skill.body}")
    return "\n\n".join(sections)


def build_react_hints(skill_names: list[str]) -> str:
    lines = ["在 ReAct 模式下，根据用户问题从已加载工具中选择并调用："]
    lines.append("- 简单问候可直接 finish 回复，勿调用工具")
    for skill in get_skills(skill_names):
        if skill.react_hint:
            lines.append(f"- {skill.react_hint}")
    tool_names = sorted(resolve_tool_names_for_skills(skill_names))
    if tool_names:
        lines.append(f"- 已加载工具：{', '.join(tool_names)}")
    return "\n".join(lines)


def build_capability_lines() -> list[str]:
    lines: list[str] = []
    for skill in discover_skills():
        if skill.casual_capability:
            lines.append(f"- {skill.casual_capability}")
    return lines


def match_skills_by_keywords(text: str) -> list[str]:
    lowered = (text or "").lower()
    matched: list[str] = []
    for skill in discover_skills():
        if any(keyword in lowered for keyword in skill.keywords):
            matched.append(skill.name)
    return matched


def resolve_skills_for_query(
    query_text: str,
    *,
    intent: str | None = None,
) -> list[str]:
    """按关键词匹配 Skill；未命中时按意图或全量 Skill 回退。"""
    matched = match_skills_by_keywords(query_text)
    if matched:
        return matched
    if intent == "query_product_attrs":
        return ["product-attrs", "knowledge"]
    return all_agent_skill_ids()
