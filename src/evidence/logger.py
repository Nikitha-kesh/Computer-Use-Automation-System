"""
Structured evidence logging for both discovery and replay runs.

Every run (discovery or replay) gets its own directory under evidence/:

    evidence/<run_id>/
        run.jsonl          # one structured event per line (append-only)
        screenshots/       # captured on step failures and at key transitions
        meta.json          # run summary written at the end

All values passed through `log_event` are redacted via the safety Redactor
before they touch disk, so PII/secrets never persist into evidence even if
they appear in an action's arguments.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.safety.policy import Redactor


@dataclass
class EvidenceLogger:
    run_id: str
    run_type: str  # "discovery" | "replay"
    base_dir: Path
    redactor: Redactor = field(default_factory=Redactor)
    _events: list[dict] = field(default_factory=list, init=False)

    @classmethod
    def create(cls, run_type: str, base_dir: str = "evidence", redactor: Redactor | None = None) -> "EvidenceLogger":
        run_id = f"{run_type}-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        d = Path(base_dir) / run_id
        (d / "screenshots").mkdir(parents=True, exist_ok=True)
        return cls(run_id=run_id, run_type=run_type, base_dir=d, redactor=redactor or Redactor())

    def log_event(self, event_type: str, **fields: Any) -> None:
        safe_fields = self.redactor.redact_dict(fields)
        event = {
            "ts": time.time(),
            "event": event_type,
            **safe_fields,
        }
        self._events.append(event)
        with open(self.base_dir / "run.jsonl", "a") as f:
            f.write(json.dumps(event, default=str) + "\n")

    def save_screenshot(self, page, label: str) -> str:
        path = self.base_dir / "screenshots" / f"{int(time.time() * 1000)}_{label}.png"
        try:
            page.screenshot(path=str(path))
        except Exception as e:  # noqa: BLE001 - best-effort evidence capture
            self.log_event("screenshot_failed", label=label, error=str(e))
            return ""
        return str(path)

    def write_meta(self, **summary: Any) -> None:
        safe_summary = self.redactor.redact_dict(summary)
        with open(self.base_dir / "meta.json", "w") as f:
            json.dump({"run_id": self.run_id, "run_type": self.run_type, **safe_summary}, f, indent=2, default=str)

    @property
    def dir(self) -> str:
        return str(self.base_dir)
