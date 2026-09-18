"""
CLI entrypoint.

    python -m src.cli discover --goal "..." --url "https://..." --app-id demoqa-practice-form \
        --base-url https://demoqa.com --param first_name=Jordan --param last_name=Lee ...

    python -m src.cli replay --artifact-id demoqa-practice-form__open-a-new-sub-account... \
        --param first_name=Jordan ...

Params are supplied as repeated --param name=value flags; declaring them up
front helps the discovery agent recognize which literal values it types
should be parameterized (see agent/llm_client.py SYSTEM_PROMPT), but the
agent may still parameterize other values it decides are caller-supplied.
"""

from __future__ import annotations

import argparse
import sys

from playwright.sync_api import sync_playwright

from src.agent.loop import run_discovery
from src.artifact.recorder import build_artifact
from src.artifact.store import ArtifactStore
from src.evidence.logger import EvidenceLogger
from src.replay.engine import replay
from src.safety.policy import AllowlistConfig, Redactor


def _parse_params(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--param must be name=value, got: {p!r}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def cmd_discover(args: argparse.Namespace) -> int:
    declared_params = [{"name": k, "type": "string", "description": f"user-supplied value: {v}"} for k, v in _parse_params(args.param).items()]
    pii_names = {k for k, v in _parse_params(args.param).items() if any(h in k for h in ("ssn", "dob", "phone", "email", "address"))}

    allowlist = AllowlistConfig(allowed_domains=[args.allowed_domain or _domain(args.url)])
    evidence = EvidenceLogger.create("discovery", redactor=Redactor(pii_field_names=pii_names))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        page = browser.new_page()
        try:
            outcome = run_discovery(
                page=page, goal=args.goal, target_url=args.url, app_id=args.app_id,
                declared_params=declared_params, allowlist=allowlist, evidence=evidence,
                max_steps=args.max_steps, interactive_escalation=not args.no_interactive,
            )
        finally:
            evidence.write_meta(goal=args.goal, target_url=args.url, app_id=args.app_id)
            browser.close()

    print(f"\nDiscovery finished. success={outcome.success} summary={outcome.summary!r}")
    print(f"Evidence: {evidence.dir}")

    if not outcome.success:
        print("No artifact produced (run was not successful).")
        return 1

    from src.agent.llm_client import DEFAULT_MODEL
    artifact = build_artifact(
        outcome=outcome, goal=args.goal, app_id=args.app_id, base_url=args.base_url or _domain(args.url, as_url=True),
        target_url=args.url, discovery_run_id=evidence.run_id, model_name=args.model or DEFAULT_MODEL,
    )
    store = ArtifactStore(args.artifacts_dir)
    path = store.save(artifact)
    print(f"Artifact saved: {path}")
    print(f"artifact_id={artifact.artifact_id} version={artifact.version}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    store = ArtifactStore(args.artifacts_dir)
    artifact = store.load_file(args.artifact_file) if args.artifact_file else store.load(args.artifact_id, args.version)

    args_dict = _parse_params(args.param)
    pii_names = {p.name for p in artifact.input_params if p.pii}
    allowlist = AllowlistConfig(allowed_domains=[args.allowed_domain or _domain(artifact.target.base_url)])
    evidence = EvidenceLogger.create("replay", redactor=Redactor(pii_field_names=pii_names))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        page = browser.new_page()
        try:
            result = replay(
                artifact=artifact, input_args=args_dict, page=page, allowlist=allowlist, evidence=evidence,
                interactive_escalation=not args.no_interactive,
            )
        finally:
            evidence.write_meta(artifact_id=artifact.artifact_id, version=artifact.version)
            browser.close()

    print(f"\nReplay finished. kind={result.kind.value}")
    print(f"message: {result.message}")
    print(f"outputs: {result.outputs}")
    print(f"steps: {result.steps_completed}/{result.steps_total}")
    print(f"Evidence: {evidence.dir}")
    return 0 if result.is_success() else 2


def _domain(url: str, as_url: bool = False) -> str:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if as_url:
        return f"{parsed.scheme}://{parsed.netloc}"
    return parsed.netloc


def main() -> int:
    parser = argparse.ArgumentParser(prog="cua")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="Run an LLM-driven discovery run and save the resulting artifact")
    d.add_argument("--goal", required=True)
    d.add_argument("--url", required=True)
    d.add_argument("--app-id", required=True)
    d.add_argument("--base-url")
    d.add_argument("--allowed-domain")
    d.add_argument("--param", action="append", default=[])
    d.add_argument("--max-steps", type=int, default=25)
    d.add_argument("--artifacts-dir", default="artifacts")
    d.add_argument("--model")
    d.add_argument("--headless", action="store_true", default=True)
    d.add_argument("--headed", dest="headless", action="store_false")
    d.add_argument("--no-interactive", action="store_true", help="auto-approve escalations instead of prompting (for CI/demo)")
    d.set_defaults(func=cmd_discover)

    r = sub.add_parser("replay", help="Deterministically replay a saved artifact")
    r.add_argument("--artifact-id")
    r.add_argument("--artifact-file")
    r.add_argument("--version", default="latest")
    r.add_argument("--allowed-domain")
    r.add_argument("--param", action="append", default=[])
    r.add_argument("--artifacts-dir", default="artifacts")
    r.add_argument("--headless", action="store_true", default=True)
    r.add_argument("--headed", dest="headless", action="store_false")
    r.add_argument("--no-interactive", action="store_true")
    r.set_defaults(func=cmd_replay)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
