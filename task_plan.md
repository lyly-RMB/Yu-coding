# Long-term Memory Development Implementation

## 2026-09-22 Hermes context compression quick implementation

Status: phases 1-7 implementation complete; release gate remains data-blocked

- [x] Add bounded context budget settings and `ContextGovernor`.
- [x] Integrate pre-request pruning and post-tool-result accounting into Hermes.
- [x] Add budget audit data to Hermes loop results and answer envelopes.
- [x] Add EvidenceCard-bound tool envelopes and detail pointers.
- [x] Add structured session state, anchored summaries, handoff markers, and rotation state.
- [x] Add immutable prepared-context snapshots, stable sections, and unified budget audit fields.
- [x] Persist rotation markers and inject bounded handoff summaries into the next Feishu request.
- [x] Implement one-time provider/context recovery with forced prune and retry audit.
- [x] Add deterministic compression probes, report generation, and release-size data gate.
- [x] Add focused governor/Hermes/session regression tests.
- [x] Add replay/probe evaluation and rollout gates.
- [x] Run focused tests, compilation, and diff checks.

## 2026-09-18 Knowledge retrieval vectorization and evaluation plan

Status: stage 7 evaluation infrastructure in progress; stages 0-6 complete; vector mode enabled for manual testing

- [x] Inspect existing BM25, embedding, memory, Evidence, SourceRecord, Obsidian, and evaluation paths.
- [x] Define the unified KnowledgeDocument/Chunk/index contract.
- [x] Define ACL-first hybrid retrieval, embedding cache, reindexing, fallback, and provenance rules.
- [x] Define retrieval, answer, temporal, conflict, abstention, security, cost, and release-gate evaluation.
- [x] Write `docs/projectlens-knowledge-retrieval-vector-and-evaluation-plan.md` and update the documentation map.
- [x] Write `docs/projectlens-knowledge-retrieval-vector-development-plan.md` with stage-by-stage implementation and acceptance criteria.
- [x] Add bounded knowledge retrieval settings and the lexical/vector/hybrid policy contract.
- [x] Add the initial BM25 baseline evaluation configuration without inventing metrics.
- [x] Add focused configuration, fallback, limit, and isolation tests.
- [x] Add the executable retrieval baseline runner and reproducible report hash.
- [x] Freeze the stage 0 ADR decisions before starting the index contract stage.
- [ ] Expand the locked development set from the current demo cases to 30-50 cases.
- [x] Add validated retrieval case schema and JSON/JSONL dataset fingerprinting.
- [x] Add lexical/vector/hybrid replay runner with Recall, MRR, nDCG, latency, diversity, duplicate, stale, rescue, false-positive, cache, and fallback metrics.
- [x] Add deterministic security evaluator for scope, forbidden, revoked, expired/pending, detail, and snapshot blockers.
- [x] Add citation correctness/completeness and unsupported-claim checks.
- [x] Add deterministic temporal validity, abstention, conflict, and fallback evaluation.
- [x] Add optional answer replay input to the local evaluation CLI without inventing answer metrics.
- [x] Add reproducible release report with git revision, dataset hash, run traces, security blockers, and gate decision.
- [ ] Expand the locked golden set to at least 100 cases and run answer/temporal/conflict evaluation before release.

## 2026-10-12 Answer pipeline responsibility refactor

Objective: separate Hermes presentation from AnswerDraft evidence verification. Hermes may
answer in any user-requested format, including 产品/研发/测试 sections. AnswerDraft only
normalizes facts, citations, unknowns, and actions; ProjectAnswer retains both the complete
answer text and the verified evidence projection; Feishu renders the complete answer first.

| Phase | Status | Deliverable |
| --- | --- | --- |
| 1. Contract audit | complete | Freeze the boundaries and identify every answer projection. |
| 2. Runtime capture | complete | Preserve Hermes raw/final answer text alongside the evidence draft. |
| 3. Evidence draft cleanup | complete | Remove role-format concerns from AnswerDraft; support nested evidence payloads and keep facts/citations/unknowns/actions only. |
| 4. Verification model | complete | Add complete answer text to ProjectAnswer without weakening evidence validation; reject ordinal citation guessing. |
| 5. Presentation | complete | Render one answer_text by default; role views are explicit-only and long answers no longer get duplicate projections. |
| 6. Regression | complete | Cover ordinary Q&A, three-role summary, malformed/uncited output, API, card, and Feishu text paths. |
| 7. Real Feishu acceptance | in_progress | Backend restarted with the latest renderer/citation changes; user must manually retest and provide chat results. |

### Target contracts

```text
Hermes raw/final response: presentation source of truth
AnswerDraft: facts + inferences + citations + unknowns + next_actions
ProjectAnswer: answer_text + verified claims/evidence + unknowns/actions
Feishu card/text: answer_text first, verification details second
```

### Non-goals

- Do not force every question into 产品/研发/测试 sections.
- Do not treat answer formatting as evidence metadata.
- Do not bypass citation verification because Hermes supplied prose.
- Do not have the card renderer invent or rewrite the model's answer.

## 2026-09-18 Knowledge retrieval implementation

| Stage | Status | Deliverable |
| --- | --- | --- |
| 0. Configuration and baseline | complete | Retrieval policy, safe defaults, ADR, baseline runner, focused tests. |
| 1. Knowledge index contract | complete | Stable document/chunk/embedding/job contracts, adapters, SQLite schema, and isolation tests. |
| 2. Chunk and provenance projection | complete | Stable chunk builder, heading/provenance metadata, overlap, and status filtering. |
| 3. Vector index and incremental jobs | complete | Validated embedding service, cache reuse, jobs, failures, and model-version isolation. |
| 4. Evidence hybrid retrieval | complete | ACL-first optional vector channel, trace fields, fallback, and application wiring. |
| 5. Unified source-layer retrieval | complete | Evidence/Memory/Wiki/History result facade with authority markers and state wiring. |
| 6. Detail, verifier, and snapshot replay | complete | Revalidate current details, verify Evidence-backed claims, persist/replay knowledge snapshots. |
| 7. Evaluation platform and quality gates | in_progress | Case schema, retrieval/security/citation/temporal/abstention/conflict/fallback evaluators, replay report, and insufficient-data gate. |
| 8. Shadow mode and production rollout | deferred | Requires a locked golden set and successful stage 7 release gate. |

## 2026-09-18 Stage 7 Stop Point

Stage 7's executable evaluation infrastructure is implemented, but the stage is
not release-complete because the repository only has a 12-case development set
and no real answer replay dataset.
Stage 8 remains deferred.

Manual testing configuration is currently:

- `PROJECT_LENS_KNOWLEDGE_RETRIEVAL_MODE=hybrid`
- `PROJECT_LENS_KNOWLEDGE_VECTOR_ENABLED=true`
- shared OpenAI-compatible embedding credentials/model reused when dedicated
  embedding credentials are not supplied
- vector provider failure falls back to lexical retrieval

## 2026-09-16 Scope

Implement the first executable slices of `docs/projectlens-long-term-memory-development-spec.md` on top of the existing ProjectLens memory stack.

| Phase | Status | Deliverable |
| --- | --- | --- |
| 1. Contract and identity | complete | Lifecycle enums, source refs, trusted actor DTO, fact-key normalization. |
| 2. Storage and migration | complete | SQLite namespaced migrations, indexed columns, FTS metadata, transactions, replacement lifecycle APIs. |
| 3. Authorized retrieval | complete | Candidate/detail authorization and application retrieval service. |
| 4. Review primitives | complete | Idempotent in-memory review queue and service contracts. |
| 5. Production integration | complete | Hermes/tool/read routes, trusted Proposal/provenance paths, durable review triggers/APIs, and Context Snapshot freeze/replay. |
| 6. Evaluation and security gates | complete | Structured memory/security/temporal case sets, deterministic case validation, and production memory quality gates. |

# Hermes-only Migration Implementation

## Objective
Implement the Hermes-only production Agent runtime migration described in `docs/hermes-only-migration-plan.md`, preserving ProjectLens authorization, evidence verification, answer rendering, and audit boundaries.

## Phases

| Phase | Status | Deliverable |
| --- | --- | --- |
| 1. Baseline | complete | Mapped runtime composition, entry points, contracts, and tests. |
| 2. Runtime core | complete | Shared `HermesRuntimeService`, run audit fields, verified output, and fail-closed behavior. |
| 3. Entry migration | complete | HTTP, Feishu natural/debug/card asks, and MCP use the shared Hermes runtime with trusted actor binding. |
| 4. Configuration | complete | Production app is bound to Hermes; `ask_run_service` and request-level bridge flags are removed. |
| 5. Verification | complete | Hermes/MCP/Feishu/HTTP/RoleView/profile migration suite passes; syntax and diff checks pass. |

## Decisions

- Hermes makes Agent decisions; ProjectLens remains the authorization, evidence, verifier, and audit kernel.
- Production failures must fail closed rather than routing into legacy runtimes.
- Existing unrelated worktree changes are preserved.
- Use `ActorContext` and `EffectiveAccessScope` as the shared runtime contract; do not introduce caller-controlled authorization fields.
- Keep `Settings.agent_mode` permissive only for isolated legacy test factories; `main.create_app()` always constructs the production run service in Hermes mode.

## Errors

| Error | Attempt | Resolution |
| --- | --- | --- |
| Initial planning skill path was incorrect. | 1 | Read the configured skill from `C:\Users\Administrator\.codex\skills`. |
| `python` was not available on PATH. | 1 | Use the configured local Python 3.12 executable for verification. |
| Sandbox denied the local Python executable. | 1 | Request permission to run the focused pytest suite with the configured executable. |
| Strict `Literal["hermes"]` rejected a developer `.env` before pytest initialization. | 1 | Updated `.env` to Hermes and kept the settings field fixture-compatible while `create_app()` remains Hermes-only. |

## Obsidian Vault Phase 0/1

### Objective

Implement the first two delivery milestones from `docs/projectlens-obsidian-hermes-development-plan.md`: freeze the secure Vault contract and export ACL-filtered ProjectLens sources and Wiki drafts as a read-only Obsidian Vault.

| Phase | Status | Deliverable |
| --- | --- | --- |
| 0. Vault contract | complete | Config, safe paths, frontmatter, manifest, and filesystem safety boundary. |
| 1. Read-only export | complete | Sources, Wiki drafts, review pages, navigation, API, and focused tests. |
| 2. Hermes Wiki read | complete | Manifest-bounded Wiki repository, search/read service, Hermes tool contracts, and focused tests. |
| 3. Knowledge operations | complete | Audited sync/export/lint operations, Lint findings, Hermes tools, and focused tests. |
| 4. Inbox proposals | complete | Controlled Inbox import and approval-bound proposal flow. |
| 5. Git/recovery operations | complete | Independent Vault Git workflow and recovery checks. |
| 6. Verification | complete | Focused regression tests, compilation, diff checks, and full-suite compatibility report. |

### Completed through Phase 5

1. Add Inbox scanner and proposal model with fixed human/Hermes proposal directories. [complete]
2. Match proposal content to authorized SourceRecord revisions and detect conflicts/duplicates. [complete]
3. Persist proposal state and expose import/propose operations without direct confirmed writes. [complete]
4. Add focused security and approval-boundary tests. [complete]

### Phase 5 Git/recovery

1. Add independent, local-only Vault Git status/diff/commit/rollback/recovery service. [complete]
2. Protect manual edits and recover corrupted manifests from the last Git commit. [complete]
3. Add export partial-write recovery and Git workflow tests. [complete]

### Phase 6 Verification

1. Phase 0-5 focused regression suite: 43 passed. [complete]
2. Source compilation: passed. [complete]
3. `git diff --check`: passed. [complete]
4. Full regression suite: 629 passed, 13 known compatibility/configuration failures recorded. [complete]
