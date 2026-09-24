# Progress Log

## 2026-09-22 Hermes context compression quick implementation

- Confirmed existing `ConversationService` already performs deterministic L1/L2/L3 compression, but Hermes still receives ungoverned context and full tool envelopes.
- Started phase 1 implementation: request-level token estimation, bounded context pruning, and audit reporting at the ProjectLens/Hermes boundary.
- Added EvidenceCard-bounded Hermes tool envelopes: duplicate `source_key + revision` references are removed, full bodies are omitted, and detail pointers remain available for explicit re-read.
- Added structured session-state integration with summary versioning, compressed-turn anchors, rotation markers, and handoff summaries.
- Added aggressive-prune protections for structured summary sections so confirmed facts, evidence references, and decisions survive compression.
- Added immutable prepared-context snapshots with request-identity-based snapshot IDs, context hashes, renderer version, and stable section-order audit fields.
- Connected rotation recommendations to Feishu session state: high-utilization runs persist `rotation_pending`, and the next request builds and injects a bounded handoff summary.
- Handoff ID and summary version are included in Hermes context audit refs.
- Implemented Phase 6 recovery: transient provider errors, timeouts, connection failures, rate limits, and context-limit failures get at most one retry after forced aggressive pruning; auth, permission, billing, and invalid-request failures do not retry.
- Recovery audit fields survive the final post-tool-loop budget report: attempts, action, and trigger classification.
- Focused Phase 6 verification: 20 Hermes/governor tests passed.
- Implemented Phase 7 deterministic compression evaluation with recall, artifact, decision, continuation, citation, and negative probes, plus token-reduction metrics and an insufficient-data release gate.
- Added `python -m project_lens.evaluation.run_compression` and generated `evaluation/reports/context-compression-report.json`.
- Development evaluation result: 2 cases, 7/7 probes passed, token reduction 64.9%, status `insufficient_data`; 50 cases remain required before a release decision.

## 2026-09-18 Knowledge retrieval vectorization plan

- Completed stage 1 knowledge index contracts: added `KnowledgeDocument`, `KnowledgeChunk`, `EmbeddingRecord`, `KnowledgeIndexJob`, stable IDs/content hashes, five source adapters, and SQLite migrations/store for documents, chunks, embeddings, and index jobs.
- Added tenant/project partitioned queries and idempotent upserts; source revisions are part of document identity, while revoked/expired/pending-derived objects are marked non-indexable.
- Added `tests/test_knowledge_index_contracts.py` and `tests/test_knowledge_index_store.py`; stage 1 focused verification: 8 passed, compilation and `git diff --check` passed.
- Completed stage 2 chunk/provenance projection: added `KnowledgeChunkBuilder` with stable chunk IDs, overlap, Markdown heading paths, source/evidence/fact metadata, derived/historical flags, and non-indexable status filtering.
- Added `tests/test_knowledge_chunking.py`; stage 2 focused verification: 12 passed, compilation and `git diff --check` passed.
- Completed stage 3 vector indexing slice: added validated batch embedding generation, cache reuse, model-version isolation, missing-job enqueueing, retry/dead-letter status, and provider error handling.
- Added `tests/test_knowledge_embeddings.py`; stage 3 focused verification: 16 passed, compilation and `git diff --check` passed.
- Completed stage 4 Evidence hybrid retrieval: ACL-first `EvidenceVectorRetriever`, optional ContextEngine vector channel, trace/fallback fields, and production wiring that keeps vector disabled by default.
- Stage 4 focused verification: 30 passed, compilation and `git diff --check` passed.
- Completed stage 5 unified source-layer facade: `SourceLayer`, `KnowledgeResult`, and `UnifiedKnowledgeRetrievalService` for Evidence, ProjectMemory, Wiki, and Episode; application state wiring added.
- Stage 5 focused verification: 13 passed, compilation and `git diff --check` passed.
- Completed stage 6 detail and replay slice: added `KnowledgeDetailService`, `KnowledgeSnapshotService`, durable SQLite snapshot payloads, hash/revision/ACL revalidation, and `KnowledgeClaimVerifier`; application state wiring added.
- Added `tests/test_knowledge_snapshot.py`; stage 6 focused verification: 3 passed, compilation and `git diff --check` passed.
- Added retrieval channels to unified results and snapshots, plus SourceRecord-backed hash/revision revalidation before replay.
- Final stages 1-6 joint regression: 53 passed, compilation and `git diff --check` passed.
- The combined command including the pre-existing `tests/test_context_snapshot.py` exposed an unrelated legacy assertion mismatch: `ContextFreezeError` text does not include `MEMORY_INVALID_PROVENANCE`. Existing behavior was left untouched.
- Stopped at stage 6 as requested. Stages 7-8 remain unimplemented.
- Enabled manual vector testing in `.env` with hybrid retrieval and vector flag on. No credentials were added or printed.
- Embedding provider construction now reuses the existing OpenAI-compatible model key/base URL when dedicated embedding key/base URL values are absent.
- Current runtime configuration check: `effective_mode=hybrid`, `vector_ready=True`, provider attached to `ContextEngine`; no network request was made during verification.
- Added 25 activation/provider regression checks; compileall and `git diff --check` passed.
- Started stage 7 evaluation platform: added validated JSON/JSONL retrieval cases, dataset fingerprints, lexical/vector/hybrid replay, retrieval metrics, security blockers, citation checks, release reports, and a local deterministic CLI.
- Added request-level `ContextQuery.retrieval_mode` so evaluation compares lexical/vector/hybrid on the same ContextEngine contract without changing the production default.
- Added vector trace fields for candidate count and embedding cache observations; ACL filtering remains before vector scoring.
- Added `evaluation/retrieval/dev_cases.jsonl`, `evaluation/configs/gates.json`, and generated `evaluation/reports/dev-retrieval-report.json`.
- Development report result: 10 cases, lexical Recall@5=1.0, hybrid Recall@5=1.0, security checks all zero, gate decision `insufficient_data`; this is not a release pass.
- Added `answer_evaluator.py` for deterministic citation correctness/completeness and unsupported fact claims; semantic answer judging remains a later calibrated layer.
- Stage 7 focused verification: 41 passed, source compilation and `git diff --check` passed.
- Added `temporal_evaluator.py` for as-of validity, stale/expired/revoked relevant-result checks, and temporal failure cases.
- Extended answer evaluation with abstention precision/recall, conflict precision/recall, case failures, and optional answer replay loading.
- Extended the local evaluation CLI with `--answers`; answer metrics remain absent unless a real answer replay file is supplied.
- Release gates now require explicit temporal, abstention, conflict, and fallback measurements once the dataset reaches the release-size threshold.
- Expanded the development retrieval set to 12 cases with one temporal case and one expected-abstention case. This remains a development fixture, not a golden release set.
- Fixed the local evaluation bootstrap to include task and release fixtures, so temporal development cases exercise real release evidence.
- Corrected security evaluation to keep stale-but-authorized temporal results as a quality signal rather than misclassifying unrelated future documents as an ACL/status security leak.
- Replayed the 12-case development report: temporal correctness=1.0, security checks all zero, lexical Recall@5=1.0, deterministic-fake hybrid Recall@5=0.875, gate decision `insufficient_data`.
- Final stage 7 continuation verification: 36 focused tests passed, report regenerated successfully, `compileall` and `git diff --check` passed.
- Remaining stage 7 work: expand the golden set to at least 100 anonymized cases and supply real answer/temporal/conflict replay records before any release decision.

- Implemented the first executable stage 0 slice: knowledge-specific retrieval settings now default to lexical mode, vector disabled, bounded candidate/result limits, versioned embedding/chunker fields, and safe vector fallback.
- Added `context/retrieval/config.py` with the `lexical` / `vector` / `hybrid` contract, vector readiness checks, mode validation, and hard upper bounds. It does not create providers or clients.
- Added `evaluation/configs/retrieval_baseline.json` as a reproducible BM25 baseline contract; metric values remain null until the evaluator runs.
- Added `tests/test_retrieval_config.py` and verified it with the existing embedding/provider safety tests: 16 passed.
- Added `evaluation/baseline.py` and `ndcg_at_k`; the baseline runner now emits a versioned JSON report with a SHA-256 config fingerprint.
- Ran the baseline against the demo payment project: 10 cases, Recall@8 1.0, MRR 1.0. This is a small lexical replay result, not a production quality claim.
- Source compilation and `git diff --check` passed. The executable baseline runner, expanded development cases, and ADR freeze remain for the rest of stage 0.
- Reviewed the current retrieval stack: `ContextEngine` is exact + BM25, while optional embedding/RRF/MMR currently covers ProjectMemory and Episode.
- Confirmed the key architectural gap is lack of a unified vectorized projection for Evidence, SourceRecord, Memory, Episode, and Obsidian Wiki.
- Wrote `docs/projectlens-knowledge-retrieval-vector-and-evaluation-plan.md`.
- The plan defines ACL-first filtering, stable chunk contracts, content/model/chunker versioned embedding cache, lexical/vector hybrid retrieval, deterministic reranking, safe fallback, provenance, and staged rollout.
- The evaluation design covers retrieval quality, answer/citation quality, temporal and conflict reasoning, abstention, ACL/security, latency, cost, and release-blocking gates.
- No production code was changed in this design-only batch.
- Wrote `docs/projectlens-knowledge-retrieval-vector-development-plan.md`, covering phases 0-8 from contract freeze through production rollout.
- Each phase specifies scope, code modules, data contracts, tests, acceptance criteria, exclusions, dependencies, and rollback behavior.

## 2026-09-16 Long-term memory development spec

- Implemented Context Snapshot freeze/replay contracts in `workflow/context_snapshot.py`.
- Memory candidates are reference-only (`memory_id`, content hash, fact key, validity, selection status); freeze rechecks project/actor/chat scope, active status, `as_of`, evidence authorization, hash, and memory budgets.
- Snapshot records preserve source refs, validity, authorization result, renderer version, and included/rejected/changed state. Hash drift raises `CONTEXT_CHANGED` and persists a changed audit snapshot.
- Added Hermes context renderer version audit metadata and focused freeze/replay/hash-mismatch tests.
- Verification: 16 focused snapshot/Hermes/memory route/review tests passed; source compilation and `git diff --check` passed.

## 2026-09-17

- Wired `ContextSnapshotService` into production app state and `HermesRuntimeService`.
- Feishu Hermes preparation now converts selected project memories into reference-only candidates; runtime freezes them before creating the run, records the snapshot ID and renderer version, and injects only the re-authorized frozen memories.
- Verification after production wiring: 9 focused Context Snapshot/Hermes tests passed; source compilation and `git diff --check` passed.

- Completed evaluation and security-gate slice from the long-term memory specification.
- Added `evaluation/memory_cases.json`, `memory_security_cases.json`, and `memory_temporal_cases.json` with the required project, temporal, abstention, conflict, forbidden-memory, and evidence fields.
- Added deterministic case loading/shape validation and security metric blockers in `evaluation/memory_cases.py`.
- Extended production release gates with optional revoked/expired/provenance/snapshot/temporal/Recall@5/citation-correctness checks while preserving legacy callers.
- Verification: 12 evaluation/retrieval/release/snapshot tests passed; Ruff and source compilation passed.

- Read `docs/projectlens-long-term-memory-development-spec.md` and mapped the existing memory domain, stores, retrieval, SQLite, and `EffectiveAccessScope` contracts.
- Baseline memory implementation is payload-only and lacks fact identity, lifecycle status, source refs, validity-aware repository APIs, schema migrations, and a centralized authorization service.
- Implementing the first executable slice: backward-compatible domain contract expansion, fact-key normalization, SQLite memory migrations/index columns, transactional boundary, InMemory/SQLite lifecycle APIs, and authorized retrieval service.
- Existing tests rely on arbitrary evidence UUIDs and legacy conflict semantics; new validation will preserve those fixtures while adding opt-in source/evidence authorization hooks.
- Added `MemoryStatus`, `ReviewReason`, `MemorySourceRef`, validity/review/source fields, and server-side fact-key generation while preserving old payload defaults.
- Added namespaced SQLite migrations, indexed memory columns, FTS source/fact metadata, a `BEGIN IMMEDIATE` transaction boundary, lifecycle APIs, and atomic SQLite replacement approval.
- Added `memory_authorization`, `MemoryRetrievalService`, `MemoryReviewQueue`, `MemoryReviewService`, and `TrustedActorContext` contracts.
- Verification: 42 focused memory/access/source tests passed; source compilation, Ruff, and `git diff --check` passed.
- Final focused verification after migration schema cleanup: 28 memory tests passed; compilation and Ruff passed.
- Continued Sprint 2 integration: Hermes `projectlens_search_project_memory` and `projectlens_get_memory_detail` now delegate to `MemoryRetrievalService`, so candidate/detail reads apply centralized project, status, validity, visibility, and source-scope authorization.
- Added v1 HTTP read routes for memory search, detail, versions, and summary; test/isolated ingress now binds their trusted actor from headers while production remains service-auth gated.
- Added `tests/test_memory_routes_v1.py`; focused route/tool/approval/Feishu/Obsidian regression reached 39 passed. Two remaining failures are pre-existing compatibility expectations for the expanded tool catalog and disabled `query_graph` surface.
- Continued Proposal integration: added strict path-based `POST /projects/{tenant_id}/{project_id}/memory-proposals` and `GET /memory-proposals/{proposal_id}` contracts, trusted actor derivation, server-side source revision resolution, and provenance error handling.
- Added `memory_provenance.py`; authoritative SourceRecord content hash/source type overwrite client metadata, revoked/missing/cross-project/unreadable refs fail closed.
- Approval gateway now uses atomic `approve_replacement()` for replacement proposals; approved memories receive deterministic content hashes when omitted.
- Verification: 16 Proposal/domain/API tests passed; Ruff and compilation passed.
- Continued review lifecycle integration: added durable `SQLiteMemoryReviewQueue`, idempotent trigger uniqueness, review list/resolve APIs, manual revoke API, and source revision/deletion hooks in `ConnectorSyncService`.
- Main application now wires `MemoryReviewService` to SQLite and connector sync; source revisions and deletions open reviews without duplicating triggers.
- Added `tests/test_memory_reviews.py` plus revoke/review API coverage. Verification: 7 review/API tests passed; broader memory/approval/source checks passed; Ruff, compilation, and `git diff --check` passed.
- Remaining spec work: production HTTP/Feishu trusted-actor proposal routes, SourceRecord-backed provenance resolution, Context Composer snapshot hash/replay integration, durable review queue service wiring, and evaluation/security case data.

## 2026-09-12

- Completed Phase 6 verification on September 15, 2026.
- Phase 0-5 Obsidian/Git focused suite: 43 passed.
- `python -m compileall -q src`: passed.
- `git diff --check`: passed.
- Full `pytest -q`: 629 passed, 13 failed. The failures are outside the Obsidian/Git phase implementation: legacy `query_graph`/MCP catalog expectations, Hermes role/card text expectations, a required `technical_summary` fixture field, and model-provider defaults overridden by the local `.env`.
- Phase 6 is complete with the compatibility failures explicitly retained for a later cleanup pass.

## 2026-09-21 Durable index build

- Added `KnowledgeIndexBuildService` for Evidence -> KnowledgeDocument ->
  KnowledgeChunk -> persisted embedding construction.
- Added incremental replacement semantics: changed content creates the current
  chunk set, old chunks become `superseded` and are excluded from vector search.
- Added source-identity retirement for Evidence updates where the upstream
  Evidence UUID changes but `(source.system, source.source_id)` remains stable.
- Added revocation handling that deactivates current chunks without deleting
  historical rows or embeddings.
- Added `KnowledgeIndexBuildResult` counters for documents, chunks, deactivated
  chunks, and embedding success/failure/cache-hit results.
- The builder is intentionally not called during application startup yet; startup
  must not unexpectedly make external embedding requests. The next integration
  step is an explicit sync/rebuild operation with bounded batches and observability.
- Verification: 17 focused index/vector tests passed; compilation and
  `git diff --check` passed.

## 2026-09-21 Explicit index synchronization

- Added `KnowledgeIndexSyncService` for explicit incremental and full snapshots.
- Added bounded batch processing, dry-run mode, invalid-mode validation, and
  synchronization result counters.
- Full sync now deactivates documents and chunks absent from the supplied source
  snapshot while retaining historical rows for audit/replay.
- Added `KnowledgeIndexStats` for document/chunk/embedding/job health:
  active counts, ready vectors, pending jobs, and failed/dead-letter jobs.
- Added synchronization tests for batching, dry-run isolation, full-snapshot
  deactivation, and status reporting.
- The sync layer is not yet wired to connector schedulers or HTTP commands; this
  remains an explicit integration step so production cannot trigger embedding work
  accidentally.
- Verification: 21 focused index/vector tests passed; compilation and
  `git diff --check` passed.

## 2026-09-21 Connector sync integration

- Injected `KnowledgeIndexSyncService` into `ConnectorSyncService`.
- Successful connector batches now trigger incremental knowledge indexing after
  Evidence/source persistence completes.
- Connector failures do not invoke the knowledge index.
- Embedding/index failures are isolated from the Evidence sync path and reported as
  `knowledge_index_error`; lexical retrieval remains available.
- Added `knowledge_indexed_count` to connector sync results.
- Registered the durable knowledge index store, builder, and sync service on
  `application.state` without performing startup indexing or network embedding calls.
- Added application integration tests for successful indexing, isolated index
  failure, and connector failure short-circuiting.
- Verification: 28 focused sync/index tests passed; app assembly smoke test passed;
  compilation and `git diff --check` passed.

## 2026-09-21 Persistent vector routing and operations API

- Switched the application vector channel from candidate-only embedding to
  `KnowledgeIndexVectorRetriever` when vector prerequisites are ready.
- Added candidate-level fallback inside `KnowledgeIndexVectorRetriever` for
  Evidence that has not yet been persisted into the chunk index, so hybrid search
  remains usable during migration and partial indexing.
- Prevented fallback from returning superseded source revisions once a source has a
  persisted current vector row.
- Added ACL-protected `POST /projects/knowledge-index/status` for project-scoped
  index health.
- Added ACL-protected `POST /projects/knowledge-index/sync` for explicit
  incremental/full rebuilds, including dry-run mode.
- Connector sync API responses now expose `knowledge_indexed_count` and
  `knowledge_index_error`.
- No vector model configuration or `.env` change was made in this pass.
- Verification: 34 focused API/sync/vector tests passed; app assembly smoke test
  confirmed `KnowledgeIndexVectorRetriever`; compilation and `git diff --check`
  passed.

## 2026-09-22 Real embedding activation and index versions

- Verified the configured OpenAI-compatible embedding provider without exposing
  credentials: provider ready, model version `qwen3.7-text-embedding`, response
  dimension 1024.
- Ran a real full build for the `demo/payment` project in an in-memory database:
  8 Evidence records, 8 chunks, 8 embeddings completed, 0 failures, 0 failed jobs.
- Ran a real hybrid query through `ContextEngine`; it returned 5 hits from 8
  persisted vector rows, with 0 fallback candidates and no vector failure.
- Added `index_version` to embedding/job contracts, settings, cache lookup/write,
  durable query filtering, and the application retriever.
- Added parallel index-version isolation coverage for v1/v2 embeddings, while
  keeping old index rows available for rollback.
- Final focused verification: 37 tests passed; compilation and `git diff --check`
  passed. No `.env` or model configuration was changed in this pass.

## 2026-09-21 Vector retrieval persistence

- Added `SQLiteKnowledgeIndexStore.list_chunks_with_embeddings()` to join only ready
  embeddings whose tenant/project, model version, chunker version, and content hash
  still match the current chunk.
- Added `KnowledgeIndexVectorRetriever` for query embedding plus application-layer
  cosine scoring over persisted vectors. It resolves results back to authorized
  Evidence candidates and filters inactive, revoked, pending, rejected, and expired
  chunks before scoring.
- Added chunk provenance for `valid_from`/`valid_to` and mapped those fields from
  Evidence metadata. This prevents expired source content from being returned by the
  durable vector path.
- Kept the default application retriever unchanged because the current app assembly
  does not yet perform a full/incremental Evidence-to-knowledge-index build. The new
  retriever is explicit and injectable; automatic production switching is deferred
  until index population and lifecycle synchronization are implemented.
- Added persistent-vector tests for provenance, tenant/candidate isolation, lifecycle
  filtering, model-version isolation, chunker-version isolation, and expiry.
- Verification: focused vector/index tests passed; `python -m compileall -q src`
  passed; `git diff --check` passed.
- Completed Phase 5 Git/recovery operations.
- Added `ObsidianGitService` for Vault-confined status, diff, commit, explicit rollback, manifest recovery checks, and manifest restoration from Git.
- Git workflow is disabled unless both Obsidian and Git flags are enabled; push and merge are intentionally not exposed.
- Export now detects manual edits through manifest/content hashes, preserves edited files as conflicts, and restores partial writes after a later file failure.
- Phase 0–5 focused verification: 43 passed; compilation and `git diff --check` passed.
- Completed Phase 4 Inbox proposal implementation.
- Wired `SQLiteKnowledgeProposalStore`, `ObsidianInboxScanner`, and `ObsidianInboxService` into `main.create_app()` behind `obsidian_enabled` and `obsidian_inbox_enabled`.
- Classified `projectlens_import_obsidian_inbox` and `projectlens_propose_wiki_update` as `propose`, with `requires_approval=true` and `allow_apply=false`.
- Added required Inbox frontmatter validation, authenticated creator binding, Evidence matching, duplicate idempotency, conflict/needs-evidence states, SQLite persistence, and `50-review/pending-approval` review pages.
- Added `tests/test_obsidian_inbox.py`; 14 tests pass.
- Focused regression result: 55 passed, 3 pre-existing Graph/MCP compatibility failures (`query_graph` rejection and legacy MCP catalog expectations).

## 2026-09-05

- Read the Hermes-only migration plan and started implementation.
- Created persistent migration tracking files.
- Recorded the existing dirty worktree as the implementation baseline; no pre-existing changes were reverted.
- A parallel probe accidentally referenced a nonexistent background exec session. It made no repository changes; subsequent direct checks succeeded.
- Inspected the existing Hermes answer adapter, bridge, RunService, entry references, and focused tests. Identified the first implementation target: a new conversational Hermes runtime service rather than the existing approval-gated patch execution service.
- Confirmed that AgentRun and the current Feishu bridge must be changed together so the real run exists before the Hermes tool loop starts.
- Mapped the existing trusted identity/scope contracts. The new runtime can reuse them directly instead of accepting authorization data from callers.
- Added Hermes audit fields to `AgentRun`, passed real run/trace IDs through the Feishu bridge lifecycle events, and added `application/hermes_runtime.py` to orchestrate create -> tool loop -> verifier result -> completion/failure.
- Verification attempt: `python` is not on this PowerShell PATH. No tests ran; retry with the known local Python 3.12 executable.
- Verification retry: the sandbox denied starting the local Python executable. Request escalated permission for the focused pytest command rather than changing the code or test scope.
- Focused Hermes suite result: 10 passed, 2 failed due to the existing test-isolation external-call guard in fake runner construction tests. No failure was caused by the new runtime code.
- Migrated production app assembly to one Hermes bridge/runtime, removed the dedicated ask RunService and request-level bridge switch, and updated the local/example environment guidance without touching credentials.
- Added runtime tests for real run/trace binding, fail-closed behavior, and HTTP trusted-actor enforcement. Focused suite now passes: 8 tests passed (one upstream TestClient deprecation warning).
- Tightened one-shot ask so it requires a trusted ActorContext; HTTP and Feishu already supply one. MCP debug ask is now fail-closed pending a verified transport identity adapter.
- Final verification: 8 focused tests passed; all changed production modules compiled; `git diff --check` reported no whitespace errors.
- Began the second migration pass: mapped MCP server/handler identity inputs and Feishu legacy tests before adding a deployment-owned MCP trusted-actor resolver.
- Resumed migration on September 5, 2026: confirmed debug/card Feishu ingress still touched the legacy run path. Implementing a prepare/execute Hermes split to close that gap while preserving synchronous run traceability.
- Focused Hermes/MCP/Feishu suite passed 32 tests. A broader migration regression found 22 stale test failures: direct `ProjectAgentAskService(run_service=...)` fixtures and Feishu integration tests expect the retired progress/read-agent path. Updating fixtures and assertions to a deterministic Hermes bridge is in progress.
- Migrated HTTP ask, RoleView, MCP profile, and Feishu integration fixtures to trusted actors plus deterministic Hermes bridges. The consolidated suite reached 60 passing tests with one remaining profile registry fixture mismatch; corrected its ProjectSpace registration to use `upsert`.
- Final verification: Hermes-only migration suite passed 61 tests; the remaining profile test passed after registry fixture correction. Changed modules compiled successfully and `git diff --check` reported no whitespace errors.

## 2026-09-08

- Implemented Phase 1 normalized source layer without replacing existing connector/Evidence contracts.
- Added immutable `SourceRecord`, `SourceType`, and `FactType` models with revision, raw URI, effective window, authority scope, supersession, related sources, ACL, hash, status, and raw content fields.
- Added append-only in-memory and SQLite source snapshot stores; duplicate `(project_id, source_id, revision)` writes are ignored and deletions are logical revocations.
- Added read-only `FeishuDocumentConnector` and exported it with the existing Bitable, Project, meeting-minute, and GitHub connectors.
- Extended `ConnectorSyncService` to persist source snapshots, skip duplicate revisions, preserve old revisions, retain cursors/errors for retry, and expose indexed/failed counters in sync state.
- Added fact-type authority resolution and ACL-filtered `ContextEngine.resolve_fact()` / `source_gaps()` for `missing`, `conflict`, `stale`, and `unconfirmed` states.
- Added `tests/test_phase1_source_records.py` covering fixture replay, revision idempotency, failure retry state, authority/conflict rules, ACL and cross-project isolation, and SQLite persistence.
- Verification: Phase 1 + connector/sync tests 14 passed; affected context/document/tool regression 38 passed; final Phase 1/context suite 30 passed; Python compilation and `git diff --check` passed.
- Full `pytest -q`: 584 passed, 2 pre-existing failures outside the Phase 1 files (`tests/test_harness_boundaries.py::test_feishu_engineering_card_never_offers_apply` has an encoding/text expectation mismatch; `tests/test_projectlens_mcp_server.py::test_mcp_list_projectlens_tools_matches_formal_catalog` sees an already-present extra `projectlens_list_project_files` catalog entry). `ruff` is not available on the current PATH; targeted Python compilation and `git diff --check` remain green.
- Continued Phase 1 application integration: SQLite `SourceRecordStore` is now injected into `ContextEngine` and Feishu document sync; added ACL-filtered `/projects/fact-resolution`, `/projects/source-gaps`, and `/projects/source-records` endpoints with evidence refs and revision/status/update metadata.
- Preserved legacy Feishu document sync behavior for isolated service callers while production app wiring keeps immutable source snapshots.
- Verification after application integration: 19 Phase 1/context tests passed; 25 Feishu doc sync/status/tool tests passed; modified modules compiled and `git diff --check` passed.

- Reframed the product direction from developer project Q&A to a cross-department project assistant for one real business/product/development/test collaboration group.
- Confirmed first pilot behavior: respond only when @mentioned or explicitly asked; retrieve Feishu documents, Bitable records, meeting materials, and GitHub evidence; explain current scope and existing development/test arrangements; report the most credible conclusion with sources; record unresolved gaps for administrator follow-up.
- Decided on original-source preservation plus LLM Wiki derived drafts. High-impact facts require confirmation before becoming current project consensus; low-risk summaries and gap reports may publish automatically.
- Decided to defer complex GraphRAG. The first pilot prioritizes source governance, authority-by-fact-type, Evidence/RAG retrieval, conflict detection, and knowledge-gap closure.
- Clarified the product decision: GraphRAG is explicitly out of scope, not merely deferred. Cross-source relationships will use structured metadata, source links, versions, and statuses.
- Confirmed the pilot implementation mode: real read-only Feishu Docs, Feishu Bitable, stored meeting materials, and GitHub connectors, with local fixtures for tests and replay.

## 2026-09-12

- Began the Obsidian Vault Phase 0/1 implementation requested from the new knowledge-base and development-plan documents.
- Preserved the existing dirty worktree and prior completed Hermes-only tracking records. Scope is limited to secure Vault configuration and read-only export; no Inbox, approval, or Git push behavior will be added in this pass.
- Added `docs/projectlens-pilot-development-spec.md`, covering source normalization, sync, LLM Wiki compilation, authority rules, Hermes tool flow, knowledge gaps, APIs, security, testing, and phased delivery.
- Added `docs/projectlens-pilot-product-blueprint.md` as the product and pilot contract for the next implementation phase.
- Read the Obsidian architecture and development-plan documents, then inspected existing source, Wiki, configuration, API, and application-composition seams.
- Starting the Vault-contract batch: independent Obsidian package plus disabled-by-default settings and focused security/serialization tests. Export, API, Inbox, and Git workflow remain out of scope for this batch.
- Completed Obsidian Vault Phase 0: added disabled-by-default settings, fixed layout contracts, allowed-root confinement, traversal and protected-file rejection, content size and secret scanning, deterministic frontmatter handling, and an atomic project-scoped manifest store.
- Verification passed: 25 focused Obsidian, Wiki publisher, and SourceRecord tests; Python compilation; and `git diff --check`. One existing FastAPI TestClient deprecation warning remains unrelated to this work.
- Completed Obsidian Phase 1 read-only export: added ACL-filtered source mirrors, Bitable current views, Wiki Markdown pages, Home/Project Overview navigation, review and knowledge-gap pages, atomic content writes, manifest updates, and `POST /projects/obsidian/export` with disabled-by-default behavior.
- Phase 1 verification passed: 29 focused Obsidian, Wiki, SourceRecord, and API tests; Python compilation; and `git diff --check`. One existing Starlette/httpx TestClient deprecation warning remains.
- Continuing directly into Phase 2: manifest-bounded Wiki search/read for Hermes, with derived/source-key/status metadata and no arbitrary filesystem access.
- Completed Obsidian Phase 2: added manifest-bounded Wiki repository search/read, project/frontmatter tamper checks, bounded page reads, conditional Hermes catalog registration, `projectlens_search_wiki`, `projectlens_read_wiki_page`, role-policy wiring, and Hermes profile guidance.
- Phase 2 verification passed: 37 focused Obsidian, SourceRecord, Wiki, API, and Hermes profile tests; Python compilation; and `git diff --check`. Two pre-existing Graph/MCP compatibility failures remain outside this feature.
- Starting Phase 3: audited knowledge operations for sync/export/Lint, with operation status and audit persistence and no fallback that can overwrite Evidence on Vault failure.
- Completed Phase 3 core: added Obsidian Lint findings, durable `KnowledgeOperation` records, sync/export/Lint orchestration with dry-run and export-failure isolation, conditional Hermes operation tools, and knowledge-operation APIs/status lookup.
- Phase 3 verification passed: 21 focused Obsidian, operation, export, Wiki, and contract tests; Python compilation; and `git diff --check`.
- Starting Phase 4: controlled Inbox proposal import with duplicate detection, Evidence matching, conflict state, and approval boundary.
