from project_lens.agent.hermes_answer_parser import parse_hermes_answer
from project_lens.agent.hermes_answer import project_answer_from_hermes
from project_lens.domain.models import ProjectRef
from project_lens.integrations.feishu.hermes_loop_runner import HermesLoopToolCall


def test_parse_structured_hermes_answer_keeps_citations_for_later_verification() -> None:
    draft = parse_hermes_answer(
        '{"facts":[{"text":"owner is Ada","citations":["ev-1"]}],'
        '"inferences":["may affect checkout"],"next_actions":["confirm"]}'
    )

    assert draft.facts[0].text == "owner is Ada"
    assert draft.facts[0].citations == ["ev-1"]
    assert draft.inferences == ["may affect checkout"]


def test_parse_plain_text_does_not_promote_text_to_verified_fact() -> None:
    draft = parse_hermes_answer("The owner is probably Ada.")

    assert draft.facts == []
    assert draft.conclusion == "The owner is probably Ada."
    assert draft.unknowns


def test_parse_empty_hermes_answer_is_unknown() -> None:
    draft = parse_hermes_answer(" \n ")

    assert draft.facts == []
    assert draft.conclusion == ""
    assert draft.unknowns == ["Hermes 未返回最终答案。"]


def test_parse_hermes_answer_envelope_maps_answer_and_citation_objects() -> None:
    evidence_id = "0f131808-4463-42f2-9ca7-f700d9e73c4a"
    draft = parse_hermes_answer(
        "{"
        '"answer":"能看到项目文档，docs/README.md 已读取。",'
        f'"citations":[{{"citation_id":"{evidence_id}","source":"repository:docs/README.md"}}],'
        '"unknowns":[],'
        '"follow_up_questions":["要看哪一篇？"]'
        "}"
    )

    assert draft.facts[0].text.startswith("能看到项目文档")
    assert draft.facts[0].citations == [evidence_id]
    assert draft.business_summary.startswith("能看到项目文档")
    assert draft.next_actions == ["要看哪一篇？"]

    answer = project_answer_from_hermes(
        project=ProjectRef(tenant_id="demo", project_id="payment"),
        draft=draft,
        tool_calls=(
            HermesLoopToolCall(
                name="projectlens_read_project_file",
                arguments={},
                envelope={
                    "ok": True,
                    "evidence_refs": [
                        {
                            "id": evidence_id,
                            "kind": "document",
                            "source_uri": "repository:docs/README.md",
                            "summary": "文档地图",
                        }
                    ],
                },
            ),
        ),
    )
    assert "不足以形成带引用结论" not in answer.business_summary
    assert answer.facts
    assert answer.evidence


def test_parse_hermes_answer_markdown_maps_used_tools() -> None:
    evidence_id = "d9c8e14d-d711-4f91-9e7c-94cb016a189c"
    draft = parse_hermes_answer(
        "{"
        '"answer_markdown":"已通过 ProjectLens 只读工具取到可引用证据。\\n\\n## 取证说明",'
        f'"citations":["{evidence_id}"],'
        '"used_tools":["projectlens_read_project_file","projectlens_search_context"],'
        '"unknowns":[]'
        "}"
    )

    assert draft.business_summary.startswith("已通过 ProjectLens")
    assert draft.facts[0].citations == [evidence_id]
    assert draft.tools_used == [
        "projectlens_read_project_file",
        "projectlens_search_context",
    ]
    assert "不足以形成带引用结论" not in draft.business_summary
