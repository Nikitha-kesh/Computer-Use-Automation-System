"""
Simple filesystem-backed artifact store.

Deliberately boring: artifacts are the reviewable unit (a human or an
approval workflow reads/signs off on the JSON directly), so plain versioned
JSON files beat a database for this scope. One file per (artifact_id,
version); a `latest` pointer per artifact_id for convenience.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.artifact.schema import CapabilityArtifact


class ArtifactStore:
    def __init__(self, base_dir: str = "artifacts"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, artifact_id: str, version: str) -> Path:
        return self.base_dir / f"{artifact_id}@{version}.json"

    def _latest_path(self, artifact_id: str) -> Path:
        return self.base_dir / f"{artifact_id}@latest.json"

    def save(self, artifact: CapabilityArtifact) -> Path:
        path = self._path(artifact.artifact_id, artifact.version)
        path.write_text(artifact.model_dump_json(indent=2))
        self._latest_path(artifact.artifact_id).write_text(artifact.model_dump_json(indent=2))
        return path

    def load(self, artifact_id: str, version: str = "latest") -> CapabilityArtifact:
        path = self._path(artifact_id, version) if version != "latest" else self._latest_path(artifact_id)
        if not path.exists():
            raise FileNotFoundError(f"no artifact {artifact_id}@{version} in {self.base_dir}")
        return CapabilityArtifact.model_validate_json(path.read_text())

    def load_file(self, path: str) -> CapabilityArtifact:
        return CapabilityArtifact.model_validate_json(Path(path).read_text())

    def list_ids(self) -> list[str]:
        seen = set()
        for p in self.base_dir.glob("*@*.json"):
            seen.add(p.name.split("@")[0])
        return sorted(seen)
