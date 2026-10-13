#!/usr/bin/env python3
"""Live acceptance: FeishuHermesToolLoopBridge + real Hermes AIAgent.

Does not use Fake runner. Reuses the ProjectLens OpenAI-compatible endpoint
unless Hermes-specific credentials are configured.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    _load_dotenv(ROOT / ".env")
    hermes_env = Path(os.environ.get("HERMES_HOME", Path.home() / "AppData/Local/hermes")) / ".env"
    # Windows HERMES_HOME override
    hermes_env = Path(r"C:\Users\Administrator\AppData\Local\hermes\.env")
    _load_dotenv(hermes_env)

    os.environ.setdefault(
        "PROJECT_LENS_HERMES_REPO",
        r"C:\Users\Administrator\Desktop\hermes-agent-main",
    )
    os.environ.setdefault("PROJECT_LENS_FEISHU_HERMES_PROVIDER", "openai")
    os.environ.setdefault("PROJECT_LENS_FEISHU_HERMES_MODEL", "gpt-5.4-mini")

    # Refresh settings after env mutation
    from project_lens import config as config_mod

    config_mod.settings = config_mod.Settings()

    from project_lens.domain.models import ProjectRef
    from project_lens.main import create_app

    app = create_app()
    bridge = app.state.feishu_hermes_tool_loop_bridge
    if bridge is None:
        print("FAIL: bridge is None (flag not enabled)")
        return 1

    runner_name = type(bridge._loop_runner).__name__
    print("runner=", runner_name)
    if runner_name != "HermesAIAgentLoopRunner":
        print("FAIL: expected HermesAIAgentLoopRunner, got", runner_name)
        return 1

    project = ProjectRef(tenant_id="demo", project_id="payment")
    question = "这个项目是做什么的？请用只读工具查证后再回答。"
    chat_id = "oc_2a853a543e7e5b004a3a0bf15be52ff8"
    user_id = "live-hermes-llm-loop"

    print("question=", question)
    result = asyncio.run(
        bridge.answer(
            project=project,
            question=question,
            user_id=user_id,
            chat_id=chat_id,
        )
    )
    envelope = result.envelope
    audit = envelope.get("audit_ref") or {}
    print("ok=", result.ok)
    print("tool_names=", list(result.tool_names))
    print("tool_calls=", json.dumps(envelope.get("tool_calls") or [], ensure_ascii=False)[:800])
    print("citations=", len(envelope.get("citations") or []))
    print("evidence_refs=", len(envelope.get("evidence_refs") or []))
    print("allow_apply=", audit.get("allow_apply"))
    print("mode=", audit.get("mode"))
    print("error=", bridge.last_error)
    print("summary_prefix=", (envelope.get("answer_summary") or "")[:240])

    if not result.ok:
        print("FAIL: Hermes loop not ok")
        return 2
    if not result.tool_names:
        print("FAIL: no projectlens_* tools were called")
        return 3
    if any(not name.startswith("projectlens_") for name in result.tool_names):
        print("FAIL: non-projectlens tool used")
        return 4
    if audit.get("allow_apply") is not False:
        print("FAIL: allow_apply must be false")
        return 5
    if not (envelope.get("citations") or envelope.get("evidence_refs")):
        print("WARN: no citations/evidence_refs (may be empty search); tools did run")
    print("PASS: live Hermes LLM tool loop invoked formal projectlens_* tools")

    # Best-effort: post the same card into the bound Feishu chat for human check.
    messenger = getattr(app.state, "feishu_messenger", None)
    if messenger is not None:
        from project_lens.integrations.feishu.hermes_tool_loop import render_hermes_tool_loop_card

        card = render_hermes_tool_loop_card(result.output_markdown)
        try:
            asyncio.run(messenger.post_card(chat_id, card))
            print("feishu_card_posted=true chat_id=", chat_id)
        except Exception as exc:  # noqa: BLE001
            print("feishu_card_posted=false error=", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
