# Findings: Hermes-only Migration

## 2026-09-22 Hermes context compression

- Existing `src/project_lens/workflow/context_compression.py` preserves pinned IDs, paths, traceback snippets, and rolling summaries; reuse it instead of creating a second session-summary system.
- `HermesAIAgentLoopRunner` receives only a rendered context string and Hermes internally owns the transient transcript, so the first safe control point is before `run_conversation()` and while building the tool-loop result envelope.
- Tool calls currently retain full envelopes in the in-memory result and expose only a compact audit pointer to the session; this is the right boundary for budget accounting and EvidenceCard-style output in the next slice.
- ProjectLens must use a business context budget below the provider maximum; provider context length alone is not a safe compression trigger.
- Hermes transcript contents are bounded at the tool boundary. Full tool bodies remain available only through ProjectLens detail pointers.
- The durable continuity source is `ProjectSessionState` plus anchored incremental summaries; Hermes transcript rotation does not own business state.
- Prepared context has an immutable snapshot identity derived from materialized context, question, system prompt, schema budget, and renderer version. The content hash remains independent of request identity for deduplication and audit.
- Hermes requests are instantiated per run, so the practical rotation boundary is the Feishu session: persist `rotation_pending` after a high-utilization run, then inject a bounded handoff into the next run and clear the marker only after handoff creation.
- Provider recovery must never resend the original oversized context. The implemented retry creates a fresh Hermes agent with `force_prune=True` and records the recovery decision in the final budget audit.
- Compression quality must be judged with probes, not token reduction alone. The first fixture covers recall, artifact, decision, continuation, citation, and negative probes and reports `insufficient_data` until the dataset reaches 50 cases.
- The release gate is intentionally data-blocked at 2 cases; the current 100% probe rate is a development signal, not a production quality claim.

## 2026-09-18 Knowledge retrieval findings

- Stage 1 uses stable document IDs keyed by tenant, project, object kind, object ID, and revision. This preserves old source revisions instead of overwriting them.
- `SQLiteKnowledgeIndexStore` uses the existing namespaced migration API and keeps all reads explicitly tenant/project scoped.
- Adapters are deliberately authorization-neutral. They preserve source ACL/status metadata and mark records indexable or non-indexable; later retrieval services must still enforce authorization before scoring.
- Episode and MemoryObservation projections are marked historical; Obsidian Wiki projections are marked derived and remain lower-authority than Evidence/SourceRecord.
- Stage 2 reuses the existing dependency-free paragraph splitter but adds stable versioned chunk IDs and heading-path metadata. This keeps current indexers unchanged while making their output embeddable later.
- Chunk construction rejects pending, rejected, revoked, and expired records before they reach the vector index. Authorization is still a later query-time responsibility.
- Stage 3 uses a separate knowledge embedding cache keyed by chunk content/model/chunker versions. Provider failures are recorded through jobs and never remove lexical availability.
- Stage 4 vector scoring receives only `ContextEngine` candidates after `EvidenceAccessPolicy.filter()`. The application creates the vector retriever only when knowledge vector configuration is complete.
- Stage 5 adds source-layer semantics without flattening authority: Evidence/SourceRecord outrank approved memory, Wiki is derived, and Episode is historical. Existing tool-specific authorization remains the enforcement boundary.
- Stage 6 snapshots store only replay-critical references and metadata: object/result IDs, source layer, content hash, revision, Evidence IDs, validity, authorization result, retrieval mode, and policy/renderer versions.
- Detail replay re-reads current stores instead of trusting the original payload. Hash drift raises `CONTEXT_CHANGED`, revision drift raises `STALE_SOURCE`, and lost authorization raises `FORBIDDEN`.
- A combined legacy snapshot regression still has one pre-existing error-message expectation mismatch; the new knowledge snapshot tests are green and no legacy snapshot behavior was changed.
- Final stage 1-6 verification is green: 53 focused tests passed. Full repository regression remains 673 passed and 17 known pre-existing compatibility/environment failures.
- Manual vector activation is configuration-complete, but actual embedding availability depends on whether the configured OpenAI-compatible endpoint supports the selected embedding model. Provider errors are intentionally handled by lexical fallback.
- Stage 0 now has a standalone `RetrievalConfig` policy. It validates the public mode vocabulary, clamps operator limits, requires provider/model/version metadata before vector readiness, and falls back to lexical when vector prerequisites are incomplete.
- Knowledge retrieval settings are deliberately separate from the existing memory embedding settings so enabling semantic memory does not silently change Evidence retrieval.
- The config factory is side-effect free: it does not instantiate embedding clients or make network calls. External-call isolation remains enforced by the existing provider construction path.
- The initial baseline file records the dataset and metric schema but leaves metrics null; no retrieval quality claim is made yet.
- The executable baseline runner is now available as `python -m project_lens.evaluation.baseline`; its current demo replay reports Recall@8=1.0 and MRR=1.0 over 10 cases.
- The development set is still below the planned 30-50 cases and must be expanded before using it as a release gate.
- Stage 7 now has a deterministic evaluation platform: `RetrievalCase` validates JSON/JSONL cases, `retrieval_runner` replays lexical/vector/hybrid modes, `security_evaluator` blocks scope/status violations, and `release_report` records reproducibility metadata and gate decisions.
- The current development dataset is 10 cases. The generated report deliberately returns `insufficient_data`, even though the demo lexical and hybrid Recall@5 are both 1.0.
- Per-case reports do not persist full query text; they retain case IDs, retrieval trace, ranked source/object metadata, degraded reasons, and security-relevant fields.
- `ContextQuery.retrieval_mode` is an optional evaluation override. Existing callers continue using configured default mode, and vector failures still fall back to exact/BM25 when configured.
- Citation correctness/completeness and unsupported fact claims are deterministic checks. They do not replace semantic completeness, conflict handling, temporal reasoning, or human/LLM review.
- The local CLI supports a deterministic fake embedding provider for smoke tests and never contacts an external embedding endpoint unless a future live runner explicitly opts into one.
- Temporal evaluation is separated from general stale-result telemetry: a temporal case passes only when a relevant result is valid at `as_of` and is not stale, expired, revoked, pending, or rejected.
- Abstention and conflict metrics are answer-level measurements. They require answer replay records with `abstained` and `conflict_detected` flags; retrieval-only runs cannot manufacture these scores.
- Once the dataset reaches the release threshold, the gate requires explicit temporal, abstention, conflict, and fallback measurements instead of treating absent strata as perfect.
- The development replay now loads payment task/release fixtures. Its temporal case passes at `as_of=2026-07-21T00:00:00+00:00`; unrelated documents observed after that time remain a retrieval-quality signal, not a security leakage.
- The deterministic fake embedding provider produced hybrid Recall@5=0.875 versus lexical Recall@5=1.0 on the 12-case development fixture. This is a useful regression demonstration, not a real embedding quality measurement.
- Current stage 7 report status is intentionally `insufficient_data` with blocker `golden_cases<100`; the temporal case passes and security checks are zero.
- The workspace contains an unrelated untracked `.env.bak-20260918-144101` temporary backup; it was preserved and not read or exposed.
- Existing `ContextEngine.search()` uses `ExactCodeRetriever` plus `BM25Retriever` and `ReciprocalRankFusion`; it does not yet use embeddings for primary Evidence retrieval.
- `context/embeddings.py` already provides an OpenAI-compatible provider, SQLite cache keyed by project/object/model/content hash, and ProjectMemory/Episode scorers with safe failure behavior.
- `memory_retrieval.py` already performs lexical/semantic RRF, recency weighting, MMR, duplicate suppression, and conflict hints, but this logic is not shared with SourceRecord/Evidence/Wiki.
- `SQLiteMemoryStore.search_memories()` uses project-scoped FTS/BM25 candidate generation before authorized semantic scoring, which is the correct security pattern to generalize.
- `ContextEngine` filters Evidence before retrieval channels, so vector support must be inserted after `_access.filter()` and before fusion.
- `SourceRecord` preserves tenant/project/source/revision/content hash/access scope and is suitable as the provenance anchor for vectorized chunks.
- Obsidian Wiki is a derived, manifest-bounded reading layer and must remain a lower-authority retrieval channel than Evidence.
- The repository contains no `C:/Users/Administrator/Desktop/qa_demo` checkout; the design therefore relies on the current ProjectLens contracts rather than copying an external retriever implementation.
- The durable vector query path is now implemented in `KnowledgeIndexVectorRetriever`.
  It joins persisted chunks and embeddings by current content/model/chunker versions,
  applies lifecycle checks, and projects only back to already authorized Evidence
  candidates.
- Automatic application wiring remains intentionally deferred: the current startup
  path has no Evidence-to-knowledge-index full/incremental builder, so switching the
  default retriever before index population would create an apparently enabled but
  empty vector path.
- `KnowledgeIndexBuildService` now supplies that missing Evidence-to-index pipeline
  for explicit sync/rebuild callers. It retires old source revisions by stable
  source identity and keeps historical rows for audit/replay.
- Connector sync now invokes the explicit index sync after Evidence persistence,
  while isolating embedding failures from the source synchronization result. The
  application exposes the index store/builder/sync service on `app.state`, but does
  not build on startup.
- The application now selects the persisted vector retriever when vector config is
  ready. It falls back per candidate for not-yet-indexed Evidence, but blocks
  fallback for superseded source identities so old revisions do not re-enter hybrid
  search after a durable index update.
- Knowledge index operations are now explicit HTTP calls: status is read-only, and
  sync supports incremental/full plus dry-run. Real embedding quality still depends
  on the user-configured provider/model and must be evaluated separately.
- On September 22, 2026, the configured real embedding provider returned 1024-
  dimensional vectors successfully. The payment project full build completed 8/8
  embeddings with zero failures, and a real hybrid query used 8 persisted vector
  rows with zero fallback candidates.
- `index_version` is now part of the embedding/job/query contract. v1 and v2 can be
  stored and queried independently; model/index rotation can therefore build a
  parallel index before switching the active version.
- Full retrieval quality cannot be claimed until a versioned, role-aware golden set with at least 100 anonymized real questions exists.
- The implementation plan is intentionally staged: Evidence hybrid retrieval precedes Memory/Episode/Wiki unification, and evaluation gates precede production enablement.

## Source plan

- `docs/hermes-only-migration-plan.md` defines Hermes as the only production decision runtime.
- Required execution chain: trusted identity and scope -> Hermes runtime -> guarded tools -> AnswerDraft -> verifier -> ProjectAnswer/AudienceView.
- Legacy workflow and read-agent paths remain test/evaluation fixtures until their capabilities are migrated and regression coverage is established.

## Repository discoveries

- The worktree already contains extensive uncommitted Hermes, access-scope, answer-verification, Feishu, HTTP, MCP, and test changes. They must be preserved and treated as the working baseline.
- Newly present migration-relevant modules include `application/hermes_execution.py`, `agent/hermes_answer.py`, `agent/hermes_answer_parser.py`, `api/hermes_execution_routes.py`, and `integrations/feishu/hermes_context.py`.
- A broad text search returned no matches because the search expression was passed with incompatible PowerShell quoting. Inspect the known modules directly before judging gaps.
- `application/hermes_execution.py` is approval-gated patch execution, not the planned conversational Hermes runtime.
- `RunService.complete_external()` can persist a verified external answer, but its normal `execute()` path still branches between workflow and read-agent.
- `ProjectAgentAskService` currently rejects every RunService except `agent_mode="read_agent"`.
- `main.py` still creates a dedicated `ask_run_service` for read-agent and gates the Feishu bridge behind `feishu_use_hermes_tool_loop`.
- The bridge creates synthetic loop/trace IDs and produces an envelope itself, so its result needs to be normalized by the new shared runtime service.
- `AgentRun` currently stores only `runtime_access`; it lacks the Hermes runtime, entry mode, loop ID, context snapshot ID, and context hash required by the plan.
- Feishu currently calls the bridge first and only then calls `RunService.complete_external()`, which prevents tool lifecycle events from being associated with the real AgentRun.
- The Hermes loop runner already accepts a bounded context string and locks model execution to the `projectlens` toolset.
- `MessageAccessContext` provides the correct per-message trusted actor plus fresh scope binding for Feishu. HTTP already has a trusted `ActorContext` ingress dependency.
- `ProjectAgentRoleViewService` only needs a RunService-compatible read interface, so it can use the shared runtime service’s run service after ask migration.
- Focused Hermes regression: 10 tests passed. Two pre-existing runner-construction tests fail because pytest isolation intentionally blocks `assert_external_calls_allowed("hermes")` before their fake agent is constructed.
- `project_answer_to_envelope()` still stamps every response with `agent_mode="read_agent"`; it must derive runtime from the run record for Hermes-only responses.
- A strict `Literal["hermes"]` settings field caused legacy fixture construction to fail before pytest setup. Production is instead locked in `main.create_app()` while isolated fixtures retain explicit legacy RunService construction.
- The production source tree, `.env`, and `.env.example` no longer reference `feishu_use_hermes_tool_loop` or `ask_run_service`.
- The MCP stdio server currently has no verified transport-identity API. Its debug one-shot ask therefore fails closed unless an adapter supplies `ActorContext`; this is preferable to treating MCP tool parameters as authorization.
- The MCP stdio server can safely use an explicit injected actor resolver or deployment-owned MCP identity settings. Both formal tools and debug ask must ignore caller-supplied user/chat fields once a trusted actor is present.
- Feishu `DEBUG_PROJECT_COMMAND` and card `ask_question` still fell through to legacy `RunService.execute()`; Hermes preparation must be reusable so these callbacks can return a real run ID before background execution.

## Product direction: 2026-09-08

- The intended product is a cross-department project assistant in one real collaboration group, not a developer-only project Q&A bot.
- The first user value is shared project context: current scope, existing agreements, development arrangements, and test arrangements, with source-backed answers and explicit gaps.
- The trigger is mention-only (`@` or explicit question); proactive monitoring is out of scope for the first pilot.
- Knowledge should preserve immutable originals and maintain an LLM-derived Wiki organized by source type: requirements, Bitable data, meeting notes, development, testing, release, and project conventions.
- Authority is defined by fact type, version, effective time, and status. Meeting notes can propose changes but should not silently override formal requirement sources.
- GraphRAG is explicitly out of scope for this product. Use ProjectSpace, Evidence, ACL, RAG, structured metadata, source links, versions, statuses, and conflict/gap records instead.
- The pilot will use real read-only Feishu Docs, Feishu Bitable, stored meeting materials, and GitHub integrations, while keeping local fixtures for deterministic tests and historical replay.
- The development contract is now captured in `docs/projectlens-pilot-development-spec.md`; implementation should extend existing ProjectSpace, Evidence, ContextEngine, Hermes tools, and Feishu adapters instead of introducing parallel runtime or index stacks.

## Obsidian Vault Phase 0/1: 2026-09-12

- `docs/projectlens-obsidian-hermes-knowledge-base.md` defines Obsidian as a team reading, review, and proposal surface, while ProjectLens remains authoritative for Evidence, ACL, conflict resolution, and approval.
- `docs/projectlens-obsidian-hermes-development-plan.md` specifies that implementation begins with Vault contract freezing and a read-only export; Inbox imports, Hermes search tools, and Git automation follow in later phases.
- The repository already has reusable `SourceRecordStore`, `WikiDraftCompiler`, SQLite persistence, project registration, API patterns, and a Feishu-only `WikiPublisher` protocol. The Obsidian exporter should be a separate local publisher rather than adapting the Feishu publisher.
- Existing dirty worktree changes and the `obsidian-vault-seed/` example must be preserved. The seed is illustrative rather than an application-owned export target.
- `SourceRecordStore.all(tenant_id, project_id)` and `WikiDraftStore.list_for_project(tenant_id, project_id)` provide the ACL-filtered data boundary the later exporter can consume after the application service resolves access.
- `WikiPublisher` is a small structural interface, but the existing Feishu implementation makes network calls and has Feishu-specific identifiers. Obsidian must use an independent local publisher/service.
- `Settings` and FastAPI `application.state` follow central assembly in `main.py`; Phase 0 can remain isolated from that wiring except for additive settings fields.
## 2026-09-12 Phase 4

- Inbox proposals are accepted only from `40-inbox/human-proposals` and `40-inbox/hermes-proposals`.
- Required metadata is enforced: `proposal_id`, tenant/project scope, `kind`, `status=proposed`, `author`, `created_at`, `evidence_ids`, and `approval_required=true`.
- `approved_by`, `confirmed`, and `decided_by` are rejected in Inbox frontmatter. The authenticated caller, not the document author, becomes `KnowledgeProposal.created_by`.
- Imported proposals are persisted by `(source_path, source_hash)` and receive a review page under `50-review/pending-approval/`.
- Hermes proposal tools remain `allow_apply=false` and `requires_approval=true`; no `ProjectMemory` write path is called.
- Remaining focused-suite failures are pre-existing migration compatibility failures around rejected `query_graph` and legacy MCP catalog expectations.

## 2026-09-15 Phase 6

- Focused Obsidian/Git regression remains green: 43 passed.
- Full regression is not fully green: 629 passed and 13 failed.
- The 13 failures are outside the Phase 0-5 Obsidian/Git behavior: legacy Graph/MCP expectations, Hermes audience/card text expectations, a fixture that omits required `technical_summary`, and provider-default tests affected by the local `.env` selecting OpenAI.
- No Phase 0-5 failure was observed, and compilation plus `git diff --check` passed.
