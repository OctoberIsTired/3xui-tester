from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Checkpoint:
    experiment_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    completed_keys: set[str] = field(default_factory=set)
    failed: int = 0

    @classmethod
    def load(cls, path: Path) -> "Checkpoint | None":
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data["experiment_id"], data.get("metadata", {}), set(data.get("completed_keys", [])), int(data.get("failed", 0)))

    def save(self, path: Path) -> None:
        payload = {"experiment_id": self.experiment_id, "metadata": self.metadata,
                   "completed": len(self.completed_keys), "failed": self.failed,
                   "completed_keys": sorted(self.completed_keys)}
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
