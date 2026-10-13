from project_lens.integrations.feishu.commands import (
    FeishuProjectCommand,
    classify_feishu_command,
    is_context_manager_command,
    rewrite_command_to_question,
    rewrite_feishu_message,
    strip_context_manager_prefix,
)
from project_lens.workflow.skills import ProjectSkill, classify_project_question


def test_classifies_project_command_shortcuts() -> None:
    assert classify_feishu_command("介绍一下这个项目") == FeishuProjectCommand.PROJECT_INTRO
    assert classify_feishu_command("这个项目是做什么的") == FeishuProjectCommand.PROJECT_INTRO
    assert classify_feishu_command("这个项目有哪些核心模块") == FeishuProjectCommand.PROJECT_INTRO
    assert classify_feishu_command("项目地图") == FeishuProjectCommand.PROJECT_MAP
    assert classify_feishu_command("最近变更") == FeishuProjectCommand.RECENT_CHANGES
    assert classify_feishu_command("最近故障") == FeishuProjectCommand.RECENT_INCIDENTS
    assert classify_feishu_command("知识库缺什么") == FeishuProjectCommand.KNOWLEDGE_GAPS
    assert classify_feishu_command("负责人是谁") == FeishuProjectCommand.OWNER_LOOKUP
    assert classify_feishu_command("文档同步状态") == FeishuProjectCommand.DOC_SYNC_STATUS
    assert classify_feishu_command("知识库最近更新了什么") == FeishuProjectCommand.DOC_SYNC_STATUS
    assert classify_feishu_command("帮我看看这个接口") == FeishuProjectCommand.FREE_QUESTION


def test_rewrites_shortcuts_to_stable_read_only_questions() -> None:
    intro = rewrite_command_to_question(FeishuProjectCommand.PROJECT_INTRO, "介绍一下这个项目")
    project_map = rewrite_command_to_question(FeishuProjectCommand.PROJECT_MAP, "项目地图")
    changes = rewrite_command_to_question(FeishuProjectCommand.RECENT_CHANGES, "最近变更")
    incidents = rewrite_command_to_question(FeishuProjectCommand.RECENT_INCIDENTS, "最近故障")
    gaps = rewrite_command_to_question(FeishuProjectCommand.KNOWLEDGE_GAPS, "知识库缺什么")
    owner = rewrite_command_to_question(FeishuProjectCommand.OWNER_LOOKUP, "负责人是谁")

    assert classify_project_question(intro) == ProjectSkill.PROJECT_KNOWLEDGE
    assert "项目定位" in intro
    assert classify_project_question(project_map) == ProjectSkill.ARCHITECTURE
    assert classify_project_question(changes) == ProjectSkill.VERSION_CHANGE
    assert classify_project_question(incidents) == ProjectSkill.INCIDENT_DIAGNOSIS
    assert classify_project_question(gaps) == ProjectSkill.PROJECT_KNOWLEDGE
    assert classify_project_question(owner) == ProjectSkill.PROJECT_KNOWLEDGE
    assert "负责人" in owner


def test_free_question_is_preserved() -> None:
    question = "create_order 这个函数怎么实现的？"

    assert rewrite_feishu_message(question) == question
    assert rewrite_feishu_message("负责人是谁").startswith("这个项目的负责人是谁")
    assert "介绍一下这个项目" in rewrite_feishu_message("这个项目是做什么的")


def test_context_manager_is_explicit_and_one_shot() -> None:
    for text in ("/context", "/context 介绍一下项目", "上下文管理：项目地图"):
        assert is_context_manager_command(text)
    assert not is_context_manager_command("普通问题里的上下文管理")
    assert classify_feishu_command("/context 介绍一下项目") == FeishuProjectCommand.CONTEXT_MANAGER
    assert strip_context_manager_prefix("/context 介绍一下项目") == "介绍一下项目"
    assert strip_context_manager_prefix("上下文管理：项目地图") == "项目地图"
