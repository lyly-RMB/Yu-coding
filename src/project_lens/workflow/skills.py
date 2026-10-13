"""Project skill routing kept independent from retrieval and presentation."""

from __future__ import annotations

from enum import StrEnum

from project_lens.context.retrieval.exact import parse_traceback


class ProjectSkill(StrEnum):
    ARCHITECTURE = "architecture"
    VERSION_CHANGE = "version_change"
    CODE_EXPLANATION = "code_explanation"
    INCIDENT_DIAGNOSIS = "incident_diagnosis"
    PROJECT_KNOWLEDGE = "project_knowledge"


def skill_routing_enabled() -> bool:
    """Whether legacy ProjectSkill routing is active in live call sites.

    Modules and helpers remain available for tests / opt-in. Production Hermes
    path keeps this off by default via Settings.project_skill_routing_enabled.
    """

    from project_lens.config import settings

    return bool(settings.project_skill_routing_enabled)


_ARCHITECTURE_TERMS = (
    "架构",
    "依赖",
    "模块",
    "服务关系",
    "architecture",
    "dependency",
    "module",
    "service relation",
)
_VERSION_TERMS = (
    "版本",
    "发布",
    "上线",
    "变更",
    "release",
    "version",
    "commit",
    "pull request",
    "pr",
)
_INCIDENT_TERMS = (
    "故障",
    "最近故障",
    "报错",
    "异常",
    "错误",
    "影响范围",
    "故障",
    "报错",
    "异常",
    "错误",
    "影响范围",
    "incident",
    "outage",
    "error",
    "exception",
    "failed",
    "failure",
)
_CODE_TERMS = (
    "代码",
    "函数",
    "方法",
    "实现",
    "code",
    "function",
    "method",
    "explain",
)
_OWNER_LOOKUP_TERMS = (
    "负责人是谁",
    "谁负责",
    "owner是谁",
    "owner 是谁",
    "缺少哪些负责人资料",
)
_PROJECT_INTRO_TERMS = (
    "介绍一下这个项目",
    "介绍这个项目",
    "介绍一下项目",
    "这个项目是做什么的",
    "项目是做什么的",
    "这个项目有哪些核心模块",
    "有哪些核心模块",
    "这个项目有哪些服务",
    "这个项目有哪些文档",
    "项目简介",
    "项目介绍",
    "please introduce this project",
    "what does this project do",
    "introduce this project",
)


def is_owner_lookup_question(question: str) -> bool:
    """Detect owner-lookup intent for specialist and Feishu card focus views."""

    normalized = "".join(question.casefold().strip().split())
    return any("".join(term.casefold().split()) in normalized for term in _OWNER_LOOKUP_TERMS)


def is_project_intro_question(question: str) -> bool:
    """Detect project-introduction intent for specialist and Feishu intro cards."""

    normalized = "".join(question.casefold().strip().split())
    return any("".join(term.casefold().split()) in normalized for term in _PROJECT_INTRO_TERMS)


def classify_project_question(question: str) -> ProjectSkill:
    """Choose a bounded read-only skill using explicit, inspectable routing rules."""
    frames, exception = parse_traceback(question)
    if frames or exception:
        return ProjectSkill.INCIDENT_DIAGNOSIS

    normalized = question.casefold()
    if any(term in normalized for term in _INCIDENT_TERMS):
        return ProjectSkill.INCIDENT_DIAGNOSIS
    # Intro questions may contain “模块/服务”; keep them on project_knowledge.
    if is_project_intro_question(question):
        return ProjectSkill.PROJECT_KNOWLEDGE
    if any(term in normalized for term in _ARCHITECTURE_TERMS):
        return ProjectSkill.ARCHITECTURE
    if any(term in normalized for term in _VERSION_TERMS):
        return ProjectSkill.VERSION_CHANGE
    if any(term in normalized for term in _CODE_TERMS):
        return ProjectSkill.CODE_EXPLANATION
    return ProjectSkill.PROJECT_KNOWLEDGE
