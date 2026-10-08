from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel


log = logging.getLogger("githerd.journal")


class JournalEntry(BaseModel):
    repo: str
    op: str
    before_head: str
    after_head: str
    branch: str | None = None  # branch the operation changed; None in old journals


class OpSet(BaseModel):
    id: str
    timestamp: str
    description: str
    entries: list[JournalEntry]


class Journal:
    def __init__(self, root: Path):
        self.dir = root / ".githerd"
        self.path = self.dir / "journal.jsonl"

    def _append(self, payload: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        ignore = self.dir / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")  # keep the journal out of any repo
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")

    def _lines(self) -> list[dict]:
        if not self.path.exists():
            return []
        text = self.path.read_text(encoding="utf-8")
        lines: list[dict] = []
        for number, ln in enumerate(text.splitlines(), start=1):
            if not ln.strip():
                continue
            try:
                parsed = json.loads(ln)
            except json.JSONDecodeError:
                log.warning("skipping malformed journal line %d in %s", number, self.path)
                continue
            if isinstance(parsed, dict) and "type" in parsed:
                lines.append(parsed)
            else:
                log.warning("skipping unrecognised journal line %d in %s", number, self.path)
        return lines

    def record(self, description: str, entries: list[JournalEntry]) -> OpSet | None:
        if not entries:
            return None
        op_set = OpSet(
            id=uuid.uuid4().hex[:8],
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            description=description,
            entries=entries,
        )
        self._append({"type": "opset", **op_set.model_dump()})
        return op_set

    def mark_undone(self, op_set_id: str) -> None:
        self._append({"type": "undone", "id": op_set_id})

    def last_undoable(self) -> OpSet | None:
        lines = self._lines()
        undone = {ln["id"] for ln in lines if ln["type"] == "undone"}
        for line in reversed(lines):
            if line["type"] == "opset" and line["id"] not in undone:
                return OpSet(**{k: v for k, v in line.items() if k != "type"})
        return None
