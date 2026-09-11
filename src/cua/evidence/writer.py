"""Run evidence: a structured log plus a richer signal on failure.

Everything written here passes through the redactor first. That is enforced by
making `log()` the only way to write, rather than by remembering to scrub at
each call site — controls that depend on discipline fail the first time
someone is in a hurry.

Layout of one run:

    evidence/<run_id>/
        run.jsonl          one JSON object per event, append-only
        result.json        the final ReplayResult
        screenshots/       NNN_label.png
        dom.html           every frame's HTML, captured on failure
        trace.zip          Playwright trace, captured on failure
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cua.policy.redaction import Redactor


class EvidenceWriter:
    def __init__(self, run_id: str, base_dir: Path, redactor: Redactor | None = None):
        self.run_id = run_id
        self.dir = Path(base_dir) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.dir / "run.jsonl"
        self.redactor = redactor or Redactor()
        self._seq = 0

    def log(self, event: str, **fields: Any) -> None:
        """Append one structured event. Redaction is not optional here."""
        self._seq += 1
        record = {
            "seq": self._seq,
            "at": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "event": event,
            **self.redactor.obj(fields),
        }
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def write_result(self, result: Any) -> None:
        payload = result.model_dump(mode="json", by_alias=True)
        (self.dir / "result.json").write_text(
            json.dumps(self.redactor.obj(payload), indent=2, default=str),
            encoding="utf-8",
        )

    def write_text(self, name: str, content: str) -> str:
        path = self.dir / name
        path.write_text(self.redactor.text(content) or "", encoding="utf-8")
        return name

    def read_events(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
