from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from project_lens.domain.models import (
    Claim,
    ClaimType,
    Evidence,
    EvidenceGrade,
    EvidenceType,
    ProjectAnswer,
    ProjectRef,
    SourceRef,
)


def make_project() -> ProjectRef:
    return ProjectRef(tenant_id="demo", project_id="payment", service="order-service")


def make_evidence() -> Evidence:
    return Evidence(
        type=EvidenceType.COMMIT,
        project=make_project(),
        source=SourceRef(system="github", source_id="a8c931f"),
        content="The null guard was removed from coupon handling.",
        observed_at=datetime.now(timezone.utc),
        access_scope="project:payment:read",
        content_hash="1234567890abcdef",
    )


def test_factual_claim_may_omit_evidence() -> None:
    claim = Claim(text="The release caused the incident", type=ClaimType.FACT)
    assert claim.evidence_ids == ()
    assert claim.type == ClaimType.FACT


def test_project_answer_rejects_unknown_evidence_reference() -> None:
    evidence = make_evidence()
    claim = Claim(
        text="The release changed coupon handling",
        type=ClaimType.FACT,
        evidence_ids=(evidence.id,),
        grade=EvidenceGrade.B,
    )

    with pytest.raises(ValidationError, match="missing evidence"):
        ProjectAnswer(
            project=make_project(),
            status="investigating",
            business_summary="Some payment requests failed.",
            technical_summary="Coupon handling changed.",
            claims=(claim,),
            evidence=(),
        )


def test_project_answer_accepts_traceable_claim() -> None:
    evidence = make_evidence()
    claim = Claim(
        text="The release changed coupon handling",
        type=ClaimType.FACT,
        evidence_ids=(evidence.id,),
        grade=EvidenceGrade.B,
    )
    answer = ProjectAnswer(
        project=make_project(),
        skill="incident_diagnosis",
        status="investigating",
        business_summary="Some payment requests failed.",
        technical_summary="Coupon handling changed.",
        claims=(claim,),
        evidence=(evidence,),
    )

    assert answer.claims[0].evidence_ids == (evidence.id,)
    assert answer.skill == "incident_diagnosis"
