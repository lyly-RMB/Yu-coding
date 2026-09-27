from uuid import UUID, uuid4

from project_lens.agent.draft import AnswerDraft, DraftFact, resolve_citation_id
from project_lens.agent.hermes_answer import project_answer_from_hermes
from project_lens.domain.models import ClaimType, ProjectRef
from project_lens.integrations.feishu.hermes_loop_runner import HermesLoopToolCall
from project_lens.integrations.feishu.views import is_degraded_answer


def _search_call(*evidence_ids: UUID) -> HermesLoopToolCall:
    return HermesLoopToolCall(
        name="projectlens_search_context",
        arguments={},
        envelope={
            "ok": True,
            "tool_name": "projectlens_search_context",
            "citations": [
                {
                    "id": str(eid),
                    "kind": "document",
                    "source_uri": f"knowledge:chunk-{i}",
                    "summary": f"hit {i}",
                }
                for i, eid in enumerate(evidence_ids)
            ],
        },
    )


def test_hermes_answer_uses_only_returned_citation_ids() -> None:
    evidence_id = uuid4()
    answer = project_answer_from_hermes(
        project=ProjectRef(tenant_id="demo", project_id="payment"),
        draft=AnswerDraft(
            facts=[DraftFact(text="owner is Ada", citations=[str(evidence_id)])]
        ),
        tool_calls=(
            HermesLoopToolCall(
                name="projectlens_search_context",
                arguments={},
                envelope={
                    "evidence_refs": [
                        {
                            "id": str(evidence_id),
                            "kind": "code",
                            "source_uri": "github:src/order.py",
                            "summary": "owner assignment",
                        }
                    ]
                },
            ),
        ),
    )

    assert answer.claims[0].evidence_ids == (evidence_id,)
    assert answer.evidence[0].metadata["citation_only"] is True


def test_hermes_answer_demotes_unknown_citation_to_unknown() -> None:
    answer = project_answer_from_hermes(
        project=ProjectRef(tenant_id="demo", project_id="payment"),
        draft=AnswerDraft(
            facts=[DraftFact(text="owner is Ada", citations=[str(uuid4())])]
        ),
        tool_calls=(),
    )

    assert answer.facts == ()
    assert answer.unknowns
    assert is_degraded_answer(answer) is True


def test_empty_citations_backfill_from_search_context_ledger() -> None:
    """Hermes answer_markdown with citations:[] must still verify via tool ledger."""

    evidence_id = uuid4()
    draft = AnswerDraft(
        facts=[
            DraftFact(
                text="已用 ProjectLens 只读工具取到可引用证据。订单服务转发支付请求。",
                citations=[],
            )
        ],
        unknowns=["负责人：fact_type=owner 返回 state: unknown"],
        business_summary="已用 ProjectLens 只读工具取到可引用证据。订单服务转发支付请求。",
        conclusion="",
        tools_used=[],
    )
    answer = project_answer_from_hermes(
        project=ProjectRef(tenant_id="demo", project_id="payment"),
        draft=draft,
        tool_calls=(_search_call(evidence_id),),
    )
    assert answer.status == "identified"
    assert answer.facts
    assert evidence_id in answer.facts[0].evidence_ids
    assert "不足以形成带引用结论" not in answer.conclusion
    assert is_degraded_answer(answer) is False


def test_short_prefix_citation_resolves_when_unique() -> None:
    evidence_id = UUID("dd3a1aa6-1111-4222-8333-444444444444")
    other = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    draft = AnswerDraft(
        facts=[
            DraftFact(
                text="核心服务依赖 payment-service（ref `dd3a1aa6`）。",
                citations=["dd3a1aa6"],
            )
        ]
    )
    answer = project_answer_from_hermes(
        project=ProjectRef(tenant_id="demo", project_id="payment"),
        draft=draft,
        tool_calls=(_search_call(evidence_id, other),),
    )
    assert answer.facts
    assert answer.facts[0].evidence_ids == (evidence_id,)
    assert is_degraded_answer(answer) is False


def test_ambiguous_short_prefix_does_not_resolve() -> None:
    first = UUID("abcd1234-1111-4222-8333-444444444444")
    second = UUID("abcd1234-9999-4222-8333-555555555555")
    evidence_ids = {first, second}
    assert resolve_citation_id("abcd1234", evidence_ids) is None
    draft = AnswerDraft(
        facts=[DraftFact(text="claim", citations=["abcd1234"])]
    )
    answer = project_answer_from_hermes(
        project=ProjectRef(tenant_id="demo", project_id="payment"),
        draft=draft,
        tool_calls=(_search_call(first, second),),
    )
    assert answer.facts == ()
    assert any("未通过引用校验" in item for item in answer.unknowns)


def test_cited_facts_with_dimension_unknowns_are_not_degraded() -> None:
    evidence_id = uuid4()
    answer = project_answer_from_hermes(
        project=ProjectRef(tenant_id="demo", project_id="payment"),
        draft=AnswerDraft(
            facts=[DraftFact(text="项目定位：订单/支付链路", citations=[str(evidence_id)])],
            unknowns=[
                "负责人：fact_type=owner 返回 state: unknown，missing authorized source",
                "无发布 / 变更 / commit / PR 类授权证据",
            ],
            business_summary="项目定位：订单/支付链路",
        ),
        tool_calls=(_search_call(evidence_id),),
    )
    assert any(c.type == ClaimType.FACT and c.evidence_ids for c in answer.claims)
    assert answer.unknowns
    assert is_degraded_answer(answer) is False


def test_prose_uuid_extracted_when_citations_array_empty() -> None:
    evidence_id = UUID("1233dba0-a73c-46c3-8069-9778d48041bb")
    draft = AnswerDraft(
        facts=[
            DraftFact(
                text=(
                    "已取到证据。依赖关系见 "
                    "`1233dba0-a73c-46c3-8069-9778d48041bb`。"
                ),
                citations=[],
            )
        ]
    )
    answer = project_answer_from_hermes(
        project=ProjectRef(tenant_id="demo", project_id="payment"),
        draft=draft,
        tool_calls=(_search_call(evidence_id),),
    )
    assert answer.facts[0].evidence_ids == (evidence_id,)
    assert is_degraded_answer(answer) is False
