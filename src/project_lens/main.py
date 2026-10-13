"""FastAPI application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI

from project_lens import __version__
from project_lens.api.trusted_actor_middleware import TrustedActorTestMiddleware
from project_lens.api.service_auth_middleware import ServiceAuthMiddleware
from project_lens.api.memory_routes import router as memory_router
from project_lens.api.project_agent_routes import router as project_agent_router
from project_lens.api.hermes_execution_routes import router as hermes_execution_router
from project_lens.api.routes import router
from project_lens.application.conversation_service import ConversationService
from project_lens.application.feishu_doc_sync import FeishuDocumentSyncService
from project_lens.application.feishu_doc_sync_scheduler import (
    FeishuDocSyncScheduler,
    attach_scheduler_to_app,
)
from project_lens.application.feishu_doc_sync_status import SQLiteFeishuDocSyncStatusStore
from project_lens.application.connector_sync import ConnectorSyncService
from project_lens.application.project_connector_factory import ProjectConnectorFactory
from project_lens.application.wiki_compiler import (
    SQLiteWikiDraftStore,
    WikiDraftCompiler,
    WikiDraftWorkflowService,
    RecordingWikiPublisher,
)
from project_lens.obsidian.errors import ObsidianError
from project_lens.obsidian.exporter import ObsidianExportService
from project_lens.obsidian.models import VaultConfig
from project_lens.obsidian.repository import ObsidianRepository
from project_lens.obsidian.lint import ObsidianLintService
from project_lens.obsidian.inbox import ObsidianInboxScanner
from project_lens.obsidian.git import ObsidianGitService
from project_lens.application.knowledge_operations import (
    KnowledgeOperationStore,
    KnowledgeOperationsService,
)
from project_lens.application.knowledge_gap_service import KnowledgeGapService
from project_lens.application.knowledge_gap_recheck import HermesKnowledgeGapVerifier, KnowledgeGapRecheckService
from project_lens.integrations.feishu.long_connection import FeishuLongConnection
from project_lens.application.knowledge_gap_store import SQLiteKnowledgeGapStore
from project_lens.application.knowledge_gap_digest import KnowledgeGapDigestService
from project_lens.application.knowledge_gap_digest_runner import (
    DigestRecipients,
    KnowledgeGapDigestRunner,
)
from project_lens.application.obsidian_inbox_service import (
    ObsidianInboxService,
    SQLiteKnowledgeProposalStore,
)
from project_lens.integrations.feishu.wiki_publisher import FeishuWikiPublisher
from project_lens.application.connector_sync_status import ConnectorSyncStateStore
from project_lens.application.hermes_runtime import HermesRuntimeService
from project_lens.application.project_agent_ask import ProjectAgentAskService
from project_lens.application.project_agent_role_view import ProjectAgentRoleViewService
from project_lens.application.project_agent_run_detail import ProjectAgentRunDetailService
from project_lens.application.project_agent_tools import ProjectAgentToolService
from project_lens.application.project_space_inspect import ProjectSpaceInspectService
from project_lens.application.run_service import RunService
from project_lens.application.risk_engine import RiskEngine
from project_lens.application.risk_feedback_service import (
    ProjectSpaceRiskFeedbackAuthorizer,
    RiskFeedbackService,
)
from project_lens.application.risk_feedback_store import (
    SQLiteHermesRiskReviewStore,
    SQLiteRiskFeedbackStore,
)
from project_lens.application.risk_notification_service import (
    ProjectRiskRecipientDirectory,
    RiskNotificationService,
)
from project_lens.application.risk_store import SQLiteRiskStore
from project_lens.application.hermes_risk_scheduler import (
    HermesRiskBackgroundScheduler,
    HermesRiskReviewScheduler,
    attach_risk_scheduler_to_app,
)
from project_lens.config import isolation_active, settings
from project_lens.context.bootstrap import (
    build_registered_context_engine,
    parse_local_project_registrations,
    to_project_registrations,
)
from project_lens.context.conversation_store import (
    ConversationStore,
    InMemoryConversationStore,
    SQLiteConversationStore,
)
from project_lens.context.memory_store import SQLiteMemoryStore
from project_lens.context.memory_review_queue import SQLiteMemoryReviewQueue
from project_lens.application.memory_review_service import MemoryReviewService
from project_lens.application.memory_retrieval_service import MemoryRetrievalService
from project_lens.application.unified_knowledge_retrieval import (
    UnifiedKnowledgeRetrievalService,
)
from project_lens.application.knowledge_snapshot import (
    KnowledgeDetailService,
    KnowledgeSnapshotService,
)
from project_lens.workflow.context_snapshot import ContextSnapshotService
from project_lens.context.source_store import SQLiteSourceRecordStore
from project_lens.context.history_store import SQLiteHistoryMemoryStore
from project_lens.context.embeddings import (
    EpisodeEmbeddingScorer,
    MemoryEmbeddingScorer,
    SQLiteEmbeddingCache,
    build_embedding_provider,
)
from project_lens.context.retrieval.config import retrieval_config_from_settings
from project_lens.context.retrieval.vector import EvidenceVectorRetriever
from project_lens.context.retrieval.knowledge_vector import KnowledgeIndexVectorRetriever
from project_lens.context.knowledge_index import (
    KnowledgeEmbeddingService,
    KnowledgeIndexBuildService,
    KnowledgeIndexSyncService,
    SQLiteKnowledgeIndexStore,
)
from project_lens.project_space.policies import ProjectRuntimeContextResolver
from project_lens.project_space.registry import (
    load_project_spaces_from_dir,
    registry_from_local_registrations,
)
from project_lens.runtime.memory_approval_gateway import MemoryApprovalGateway
from project_lens.integrations.feishu.adapter import RecordingFeishuMessenger
from project_lens.integrations.feishu.approval_routes import router as approval_router
from project_lens.integrations.feishu.docs_client import FeishuDocClient
from project_lens.integrations.feishu.http_adapter import (
    FeishuTenantTokenProvider,
    HttpFeishuMessenger,
)
from project_lens.integrations.feishu.hermes_tool_loop import FeishuHermesToolLoopBridge
from project_lens.integrations.feishu.virtual_members import VirtualMemberController
from project_lens.integrations.feishu.virtual_members import VirtualMemberController
from project_lens.integrations.hermes_plugin.config import ProjectLensPluginConfig
from project_lens.integrations.feishu.identity import parse_project_bindings
from project_lens.integrations.feishu.messages_client import FeishuMessagesClient
from project_lens.integrations.feishu.routes import router as feishu_router
from project_lens.integrations.feishu.security import FeishuRequestVerifier
from project_lens.integrations.feishu.service import FeishuEventService
from project_lens.integrations.feishu.status_routes import router as feishu_status_router
from project_lens.application.genai_trace_service import GenAITraceService
from project_lens.persistence.genai_trace_store import GenAITraceStore
from project_lens.persistence.sqlite import (
    SQLiteApprovalStore,
    SQLiteDatabase,
    SQLiteEventDeduplicator,
    SQLiteEventSink,
    SQLiteRunRepository,
    SQLiteEngineeringApprovalStore,
)
from project_lens.runtime.lifecycle import LifecycleBus
from project_lens.context.models import AccessContext
from project_lens.context.governor import ContextGovernor, ContextGovernorConfig
from project_lens.workflow.providers.factory import create_model_adapter_from_settings


def create_app(database_path: str = ":memory:") -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        scheduler: FeishuDocSyncScheduler | None = getattr(
            application.state, "feishu_doc_sync_scheduler", None
        )
        risk_scheduler: HermesRiskBackgroundScheduler | None = getattr(
            application.state, "hermes_risk_scheduler", None
        )
        digest_runner: KnowledgeGapDigestRunner | None = getattr(
            application.state, "knowledge_gap_digest_runner", None
        )
        feishu_long_connection: FeishuLongConnection | None = getattr(
            application.state, "feishu_long_connection", None
        )
        if scheduler is not None:
            scheduler.start()
        if risk_scheduler is not None:
            risk_scheduler.start()
        if digest_runner is not None:
            digest_runner.start()
        if feishu_long_connection is not None:
            feishu_long_connection.start()
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.stop()
            if risk_scheduler is not None:
                risk_scheduler.stop()
            if digest_runner is not None:
                digest_runner.stop()
            if feishu_long_connection is not None:
                feishu_long_connection.stop()

    application = FastAPI(
        title="ProjectLens API",
        description="Evidence-grounded enterprise project collaboration agent",
        version=__version__,
        lifespan=lifespan,
    )
    base_dir = Path(__file__).resolve().parents[2]
    project_registrations = parse_local_project_registrations(
        settings.local_project_registry,
        base_dir=base_dir,
    )
    database = SQLiteDatabase(database_path)
    application.state.database = database
    embedding_provider = build_embedding_provider(settings)
    embedding_cache = SQLiteEmbeddingCache(database) if embedding_provider else None
    knowledge_index_store = SQLiteKnowledgeIndexStore(database)
    knowledge_embedding_service = (
        KnowledgeEmbeddingService(knowledge_index_store, embedding_provider)
        if embedding_provider is not None
        else None
    )
    knowledge_index_builder = KnowledgeIndexBuildService(
        knowledge_index_store,
        embedding_service=knowledge_embedding_service,
    )
    knowledge_index_sync = KnowledgeIndexSyncService(
        knowledge_index_store,
        knowledge_index_builder,
    )
    application.state.knowledge_index_store = knowledge_index_store
    application.state.knowledge_index_builder = knowledge_index_builder
    application.state.knowledge_index_sync = knowledge_index_sync
    retrieval_config = retrieval_config_from_settings(settings)
    vector_retriever = (
        KnowledgeIndexVectorRetriever(
            knowledge_index_store,
            embedding_provider,
            chunker_version=retrieval_config.chunker_version,
            index_version=retrieval_config.index_version,
            fallback_retriever=EvidenceVectorRetriever(
                embedding_provider,
                cache=embedding_cache,
            ),
        )
        if (
            embedding_provider is not None
            and retrieval_config.vector_ready
            and retrieval_config.effective_mode in {"vector", "hybrid"}
        )
        else None
    )
    source_record_store = SQLiteSourceRecordStore(database)
    application.state.source_record_store = source_record_store
    wiki_draft_store = SQLiteWikiDraftStore(database)
    risk_store = SQLiteRiskStore(database)
    risk_engine = RiskEngine(risk_store)
    risk_feedback_store = SQLiteRiskFeedbackStore(database)
    context_engine, evidence_index = build_registered_context_engine(
        project_registrations,
        risk_engine=risk_engine,
        source_store=source_record_store,
        retrieval_config=retrieval_config,
        vector_retriever=vector_retriever,
    )
    application.state.context_engine = context_engine
    application.state.evidence_index = evidence_index
    knowledge_gap_store = SQLiteKnowledgeGapStore(database)
    application.state.knowledge_gap_store = knowledge_gap_store
    knowledge_gap_service = KnowledgeGapService(knowledge_gap_store)
    application.state.knowledge_gap_service = knowledge_gap_service
    application.state.knowledge_gap_digest_service = KnowledgeGapDigestService(
        knowledge_gap_store, knowledge_gap_store
    )
    application.state.local_project_registrations = project_registrations
    connector_sync_state_store = ConnectorSyncStateStore(database)
    connector_sync_service = ConnectorSyncService(
        evidence_index=evidence_index,
        state_store=connector_sync_state_store,
        source_store=source_record_store,
        freshness_max_age_seconds=settings.connector_freshness_max_age_seconds,
        knowledge_index_sync=knowledge_index_sync,
    )
    application.state.connector_sync_state_store = connector_sync_state_store
    application.state.connector_sync_service = connector_sync_service
    application.state.wiki_draft_store = wiki_draft_store
    application.state.wiki_draft_compiler = WikiDraftCompiler(
        source_record_store, wiki_draft_store
    )
    obsidian_export_service = None
    obsidian_repository = None
    if settings.obsidian_enabled:
        # Validate deployment-owned paths while assembling the app, before an
        # HTTP request can reach the local filesystem boundary.
        _obsidian_config_for_project(project_registrations[0].project, base_dir)
        obsidian_export_service = ObsidianExportService(
            source_store=source_record_store,
            wiki_store=wiki_draft_store,
            config_for_project=lambda project: _obsidian_config_for_project(project, base_dir),
        )
        obsidian_repository = ObsidianRepository(
            config_for_project=lambda project: _obsidian_config_for_project(project, base_dir),
        )
    application.state.obsidian_export_service = obsidian_export_service
    application.state.obsidian_repository = obsidian_repository
    obsidian_inbox_service = None
    if settings.obsidian_enabled and settings.obsidian_inbox_enabled:
        obsidian_inbox_scanner = ObsidianInboxScanner(
            config_for_project=lambda project: _obsidian_config_for_project(project, base_dir),
        )
        obsidian_inbox_service = ObsidianInboxService(
            scanner=obsidian_inbox_scanner,
            proposal_store=SQLiteKnowledgeProposalStore(database),
            source_store=source_record_store,
        )
    application.state.obsidian_inbox_service = obsidian_inbox_service
    obsidian_git_service = None
    if settings.obsidian_enabled and settings.obsidian_git_enabled:
        obsidian_git_service = ObsidianGitService(
            config_for_project=lambda project: _obsidian_config_for_project(project, base_dir),
        )
    application.state.obsidian_git_service = obsidian_git_service
    resolver_registrations = to_project_registrations(project_registrations)
    default_project = resolver_registrations[0].project
    event_sink = SQLiteEventSink(database)
    lifecycle = LifecycleBus(event_sink=event_sink)
    application.state.event_sink = event_sink
    application.state.lifecycle_bus = lifecycle
    # Stub/live adapter kept for observability and tests; ask runtime is Hermes-only.
    model_adapter = create_model_adapter_from_settings(settings)
    project_registry = registry_from_local_registrations(project_registrations)
    spaces_dir = base_dir / settings.project_spaces_dir
    for space in load_project_spaces_from_dir(spaces_dir, base_dir=base_dir):
        # JSON ProjectSpace overlays local registrations so file_allowlist /
        # role_policies / chat_visibility_policies from config/projects win.
        project_registry.upsert(space)
    runtime_context_resolver = ProjectRuntimeContextResolver(
        project_registry=project_registry,
    )
    run_repository = SQLiteRunRepository(database)
    history_store = SQLiteHistoryMemoryStore(
        database,
        vector_scorer=(
            EpisodeEmbeddingScorer(embedding_provider, cache=embedding_cache)
            if embedding_provider
            else None
        ),
    )
    run_service = RunService(
        run_repository,
        event_sink=event_sink,
        lifecycle=lifecycle,
        agent_mode="hermes",
        history_store=history_store,
    )
    memory_store = SQLiteMemoryStore(database)
    context_snapshot_service = ContextSnapshotService(
        memory_store=memory_store,
        source_store=source_record_store,
        evidence_lookup=lambda evidence_id: next(
            (
                item
                for item in evidence_index.all(include_revoked=True)
                if item.id == evidence_id
            ),
            None,
        ),
    )
    memory_vector_scorer = (
        MemoryEmbeddingScorer(embedding_provider, cache=embedding_cache)
        if embedding_provider
        else None
    )
    application.state.memory_retrieval_service = MemoryRetrievalService(
        memory_store,
        vector_scorer=memory_vector_scorer,
    )
    application.state.unified_knowledge_retrieval = UnifiedKnowledgeRetrievalService(
        context_engine=context_engine,
        memory_service=application.state.memory_retrieval_service,
        history_store=history_store,
        wiki_repository=obsidian_repository,
    )
    knowledge_detail_service = KnowledgeDetailService(
        context_engine=context_engine,
        source_store=source_record_store,
        memory_service=application.state.memory_retrieval_service,
        history_store=history_store,
        wiki_repository=obsidian_repository,
    )
    application.state.knowledge_detail_service = knowledge_detail_service
    application.state.knowledge_snapshot_service = KnowledgeSnapshotService(
        knowledge_detail_service,
        database=database,
    )
    memory_review_service = MemoryReviewService(
        memory_store,
        queue=SQLiteMemoryReviewQueue(database),
    )
    connector_sync_service.configure_memory_review_service(memory_review_service)
    application.state.memory_review_service = memory_review_service
    project_agent_tool_service = ProjectAgentToolService(
        context_engine=context_engine,
        project_registry=project_registry,
        memory_store=memory_store,
        run_repository=run_repository,
        event_sink=event_sink,
        history_store=history_store,
        memory_vector_scorer=memory_vector_scorer,
        obsidian_repository=obsidian_repository,
        obsidian_inbox=obsidian_inbox_service,
        advanced_tools_enabled=settings.feishu_hermes_advanced_tools,
    )
    project_space_inspect_service = ProjectSpaceInspectService(
        project_registry=project_registry,
    )
    hermes_tool_loop_bridge = FeishuHermesToolLoopBridge(
        tool_service=project_agent_tool_service,
        config=ProjectLensPluginConfig.from_env(),
        lifecycle=lifecycle,
        hermes_repo=settings.hermes_repo,
        hermes_provider=settings.feishu_hermes_provider,
        hermes_model=settings.feishu_hermes_model,
        hermes_base_url=(
            settings.feishu_hermes_base_url or settings.model_openai_base_url
        ),
        hermes_api_key=(
            settings.feishu_hermes_api_key or settings.model_openai_api_key
        ),
        context_governor=ContextGovernor(
            ContextGovernorConfig(
                enabled=settings.context_governor_enabled,
                model_context_limit_tokens=settings.context_model_limit_tokens,
                soft_limit_ratio=settings.context_soft_limit_ratio,
                summarize_ratio=settings.context_summarize_ratio,
                prune_ratio=settings.context_prune_ratio,
                rotate_ratio=settings.context_rotate_ratio,
                hard_limit_ratio=settings.context_hard_limit_ratio,
                output_reserve_tokens=settings.context_output_reserve_tokens,
                tool_schema_tokens=settings.context_tool_schema_tokens,
                provider_overhead_tokens=settings.context_provider_overhead_tokens,
                max_recovery_retries=settings.context_max_recovery_retry,
            )
        ),
    )
    genai_trace_root = Path(settings.genai_trace_dir)
    if not genai_trace_root.is_absolute():
        genai_trace_root = base_dir / genai_trace_root
    genai_trace_service = GenAITraceService(
        GenAITraceStore(database, root_dir=genai_trace_root),
        retention_days=settings.genai_trace_retention_days,
    )
    hermes_runtime_service = HermesRuntimeService(
        run_service=run_service,
        bridge=hermes_tool_loop_bridge,
        context_snapshots=context_snapshot_service,
        genai_trace_service=genai_trace_service,
        tool_catalog_provider=project_agent_tool_service,
    )
    knowledge_gap_recheck_service = KnowledgeGapRecheckService(
        knowledge_gap_service,
        HermesKnowledgeGapVerifier(hermes_runtime_service),
    )
    application.state.knowledge_gap_recheck_service = knowledge_gap_recheck_service
    project_agent_ask_service = ProjectAgentAskService(
        hermes_runtime=hermes_runtime_service,
        project_registry=project_registry,
        runtime_context_resolver=runtime_context_resolver,
        knowledge_gap_service=knowledge_gap_service,
        knowledge_gap_digest_service=application.state.knowledge_gap_digest_service,
        feishu_messenger=None,
    )
    project_agent_role_view_service = ProjectAgentRoleViewService(
        run_service=run_service,
    )
    project_agent_run_detail_service = ProjectAgentRunDetailService(
        run_service=run_service,
        project_runtime_context_resolver=runtime_context_resolver,
        genai_trace_service=genai_trace_service,
    )
    application.state.model_adapter = model_adapter
    application.state.project_registry = project_registry
    application.state.project_runtime_context_resolver = runtime_context_resolver
    application.state.run_service = run_service
    application.state.hermes_runtime_service = hermes_runtime_service
    application.state.genai_trace_service = genai_trace_service
    application.state.project_agent_ask_service = project_agent_ask_service
    application.state.project_agent_role_view_service = project_agent_role_view_service
    application.state.project_agent_run_detail_service = project_agent_run_detail_service
    application.state.project_agent_tool_service = project_agent_tool_service
    application.state.project_space_inspect_service = project_space_inspect_service
    application.state.feishu_hermes_tool_loop_bridge = hermes_tool_loop_bridge
    application.state.hermes_tool_loop_enabled = True
    application.state.approval_store = SQLiteApprovalStore(database)
    application.state.engineering_approval_store = SQLiteEngineeringApprovalStore(database)
    application.state.hermes_execution_enabled = settings.feishu_hermes_execution_enabled
    application.state.memory_store = memory_store
    application.state.context_snapshot_service = context_snapshot_service
    application.state.history_memory_store = history_store
    memory_approval_gateway = MemoryApprovalGateway(application.state.memory_store)
    application.state.memory_approval_gateway = memory_approval_gateway
    sync_status_store = SQLiteFeishuDocSyncStatusStore(database)
    application.state.feishu_doc_sync_status_store = sync_status_store
    identity_mapper = parse_project_bindings(
        settings.feishu_project_bindings,
        default_project=default_project,
        registered_projects=tuple(space.project for space in project_registry.list()),
    )
    token_provider = None
    if settings.feishu_app_id and settings.feishu_app_secret:
        token_provider = FeishuTenantTokenProvider(
            app_id=settings.feishu_app_id,
            app_secret=settings.feishu_app_secret,
            base_url=settings.feishu_api_base_url,
        )
        messenger = HttpFeishuMessenger(
            token_provider=token_provider,
            base_url=settings.feishu_api_base_url,
        )
    else:
        messenger = RecordingFeishuMessenger()
    wiki_publisher = RecordingWikiPublisher()
    if (
        settings.feishu_wiki_publish_enabled
        and token_provider is not None
        and settings.feishu_wiki_space_id
    ):
        wiki_publisher = FeishuWikiPublisher(
            token_provider=token_provider,
            space_id=settings.feishu_wiki_space_id,
            parent_node_token=settings.feishu_wiki_parent_node_token,
            base_url=settings.feishu_api_base_url,
        )
    application.state.wiki_publisher = wiki_publisher
    application.state.wiki_draft_workflow = WikiDraftWorkflowService(
        wiki_draft_store, wiki_publisher
    )
    application.state.feishu_credentials_configured = bool(
        settings.feishu_app_id and settings.feishu_app_secret
    )
    application.state.feishu_outbound_mode = (
        "http" if application.state.feishu_credentials_configured else "recording"
    )
    application.state.feishu_binding_count = identity_mapper.binding_count
    application.state.feishu_messenger = messenger
    project_agent_ask_service.configure_gap_delivery(
        digest_service=application.state.knowledge_gap_digest_service,
        messenger=messenger,
    )
    application.state.project_connector_factory = ProjectConnectorFactory(
        project_registry=project_registry,
        feishu_token_provider=token_provider,
        feishu_base_url=settings.feishu_api_base_url,
        github_token=settings.github_token,
        github_api_base_url=settings.github_api_base_url,
    )
    knowledge_operations = None
    if settings.obsidian_enabled and settings.obsidian_operations_enabled:
        knowledge_operations = KnowledgeOperationsService(
            store=KnowledgeOperationStore(database),
            connector_sync=connector_sync_service,
            connector_factory=application.state.project_connector_factory,
            wiki_compiler=application.state.wiki_draft_compiler,
            obsidian_export=obsidian_export_service,
            obsidian_lint=ObsidianLintService(
                source_store=source_record_store,
                config_for_project=lambda project: _obsidian_config_for_project(project, base_dir),
            ),
        )
    project_agent_tool_service.configure_knowledge_operations(knowledge_operations)
    application.state.knowledge_operations_service = knowledge_operations
    risk_feedback_service = RiskFeedbackService(
        risk_engine=risk_engine,
        store=risk_feedback_store,
        authorizer=ProjectSpaceRiskFeedbackAuthorizer(project_registry),
    )
    risk_notification_service = RiskNotificationService(
        messenger=messenger,
        feedback_store=risk_feedback_store,
        risk_engine=risk_engine,
        recipient_directory=ProjectRiskRecipientDirectory(project_registry),
        runtime_context_resolver=runtime_context_resolver,
    )
    application.state.risk_store = risk_store
    application.state.risk_engine = risk_engine
    application.state.risk_feedback_store = risk_feedback_store
    application.state.risk_feedback_service = risk_feedback_service
    application.state.risk_notification_service = risk_notification_service
    hermes_risk_review = HermesRiskReviewScheduler(
        risk_engine=risk_engine,
        review_store=SQLiteHermesRiskReviewStore(database),
    )
    access_scopes = {item.project: item.access_scope for item in project_registrations}
    background_risk_scheduler = HermesRiskBackgroundScheduler(
        projects=tuple(item.project for item in project_registrations),
        review_scheduler=hermes_risk_review,
        evidence_provider=lambda project: context_engine.authorized_evidence(
            project,
            AccessContext(
                tenant_id=project.tenant_id,
                user_id="hermes-background",
                permissions=frozenset({
                    access_scopes.get(project, "")
                }),
            ),
            limit=50,
        ),
        on_startup=settings.feishu_hermes_risk_review_enabled,
        interval_seconds=(
            settings.feishu_hermes_risk_review_interval_seconds
            if settings.feishu_hermes_risk_review_enabled
            else 0
        ),
    )
    attach_risk_scheduler_to_app(application, background_risk_scheduler)
    doc_client = (
        FeishuDocClient(
            token_provider=token_provider,
            base_url=settings.feishu_api_base_url,
        )
        if token_provider is not None
        else None
    )
    sync_service = FeishuDocumentSyncService(
        client=doc_client,
        index=evidence_index,
        status_store=sync_status_store,
        source_store=source_record_store,
        knowledge_gap_service=knowledge_gap_service,
    )
    def _resolve_digest_context(project, period_start, period_end):
        registration = next(
            (item for item in project_registrations if item.project == project),
            None,
        )
        access_scope = registration.access_scope if registration is not None else ""
        permissions = frozenset({access_scope}) if access_scope else frozenset()
        evidence = context_engine.authorized_evidence(
            project,
            AccessContext(
                tenant_id=project.tenant_id,
                user_id="knowledge-gap-digest",
                permissions=permissions,
            ),
            limit=50,
        )
        space = project_registry.get(project.tenant_id, project.project_id)
        owner_ids = frozenset(member.actor_id for member in (space.members if space else ()))
        owner_open_ids = {
            owner_id: open_id
            for owner_id in owner_ids
            if (open_id := identity_mapper.owner_open_id(project, owner_id))
        }
        return (
            frozenset(item.id for item in evidence),
            owner_ids,
            DigestRecipients(
                project_chat_id=identity_mapper.project_chat_id(project) or "",
                owner_open_ids=owner_open_ids,
            ),
        )

    knowledge_gap_digest_runner = KnowledgeGapDigestRunner(
        service=application.state.knowledge_gap_digest_service,
        projects=tuple(item.project for item in project_registrations),
        resolve_context=_resolve_digest_context,
        sender=messenger,
        timezone_name=settings.feishu_knowledge_gap_digest_timezone,
        enabled=settings.feishu_knowledge_gap_digest_enabled,
        interval_seconds=settings.feishu_knowledge_gap_digest_interval_seconds,
    )
    application.state.knowledge_gap_digest_runner = knowledge_gap_digest_runner
    application.state.feishu_doc_sync_service = sync_service
    attach_scheduler_to_app(
        application,
        FeishuDocSyncScheduler(
            service=sync_service,
            registrations=project_registrations,
            on_startup=settings.feishu_doc_sync_on_startup,
            interval_seconds=settings.feishu_doc_sync_interval_seconds,
            lifecycle=lifecycle,
        ),
    )
    conversation_store = _build_conversation_store(database)
    group_history_client = (
        FeishuMessagesClient(
            token_provider=token_provider,
            base_url=settings.feishu_api_base_url,
        )
        if token_provider is not None
        else None
    )
    conversation_service = ConversationService(
        store=conversation_store,
        lifecycle=lifecycle,
        group_history=group_history_client,
    )
    application.state.conversation_store = conversation_store
    application.state.conversation_service = conversation_service
    application.state.feishu_messages_client = group_history_client
    project_agent_tool_service.configure_conversation_service(conversation_service)
    application.state.feishu_event_service = FeishuEventService(
        run_service=run_service,
        verifier=FeishuRequestVerifier(
            verification_token=settings.feishu_verification_token,
            signing_secret=settings.feishu_signing_secret,
        ),
        deduplicator=SQLiteEventDeduplicator(database),
        identity_mapper=identity_mapper,
        messenger=application.state.feishu_messenger,
        memory_store=application.state.memory_store,
        memory_approval_gateway=memory_approval_gateway,
        sync_status_store=sync_status_store,
        conversation_service=conversation_service,
        lifecycle=lifecycle,
        runtime_context_resolver=runtime_context_resolver,
        hermes_tool_loop_bridge=hermes_tool_loop_bridge,
        hermes_runtime=hermes_runtime_service,
        run_detail_service=project_agent_run_detail_service,
        risk_feedback_service=risk_feedback_service,
        risk_engine=risk_engine,
        risk_notification_service=risk_notification_service,
        knowledge_gap_service=knowledge_gap_service,
        knowledge_gap_recheck_service=knowledge_gap_recheck_service,
        virtual_members=VirtualMemberController(
            enabled=settings.feishu_virtual_members_enabled and settings.env.lower() != "production",
            operators=settings.feishu_virtual_member_operators,
            profiles=settings.feishu_virtual_member_profiles,
        ),
        # Pytest isolation keeps preview on so regressions stay stable; local
        # .env can still disable it for the live uvicorn process.
        context_preview_enabled=(
            True if isolation_active() else settings.feishu_context_preview_enabled
        ),
    )
    application.state.feishu_long_connection = FeishuLongConnection(
        app_id=settings.feishu_app_id or "",
        app_secret=settings.feishu_app_secret or "",
        event_service=application.state.feishu_event_service,
        # Never open a live Feishu WS under pytest — credentials in local .env
        # would otherwise redeliver group events and double-post cards/replies.
        enabled=(
            not isolation_active()
            and settings.feishu_app_id is not None
            and settings.feishu_app_secret is not None
        ),
    )
    application.include_router(router, prefix=settings.api_prefix)
    application.include_router(project_agent_router, prefix=settings.api_prefix)
    application.include_router(hermes_execution_router, prefix=settings.api_prefix)
    application.include_router(feishu_router, prefix=settings.api_prefix)
    application.include_router(feishu_status_router, prefix=settings.api_prefix)
    application.include_router(approval_router, prefix=settings.api_prefix)
    application.include_router(memory_router, prefix=settings.api_prefix)
    if isolation_active() or settings.test_mode:
        application.add_middleware(TrustedActorTestMiddleware)
    elif settings.env.strip().lower() in {"production", "pilot"}:
        application.add_middleware(ServiceAuthMiddleware)
    return application


def _build_conversation_store(database: SQLiteDatabase) -> ConversationStore:
    backend = settings.conversation_store.strip().lower()
    if backend in {"memory", "inmemory", "in_memory"}:
        return InMemoryConversationStore()
    return SQLiteConversationStore(database)


def _obsidian_config_for_project(project, base_dir: Path) -> VaultConfig:
    """Resolve one project Vault from deployment-owned settings."""

    if settings.obsidian_enabled:
        if not settings.obsidian_vault_root or not settings.obsidian_allowed_root:
            raise ObsidianError(
                "Obsidian export requires PROJECT_LENS_OBSIDIAN_VAULT_ROOT "
                "and PROJECT_LENS_OBSIDIAN_ALLOWED_ROOT"
            )
        vault_parent = Path(settings.obsidian_vault_root).expanduser()
        allowed_root = Path(settings.obsidian_allowed_root).expanduser()
    else:
        # Keep disabled configuration constructible without touching the filesystem.
        vault_parent = base_dir / ".projectlens-obsidian"
        allowed_root = base_dir
    return VaultConfig(
        root=vault_parent / settings.obsidian_project_subdir / project.project_id,
        allowed_root=allowed_root,
        tenant_id=project.tenant_id,
        project_id=project.project_id,
        enabled=settings.obsidian_enabled,
        project_subdir=settings.obsidian_project_subdir,
        export_mode=settings.obsidian_export_mode,
        max_file_bytes=settings.obsidian_max_file_bytes,
        git_enabled=settings.obsidian_git_enabled,
    )


app = create_app(settings.database_path)
