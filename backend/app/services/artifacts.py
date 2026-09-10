"""Artifact store: writes the deliverables for a run and tracks their state.

Two responsibilities:

1. Persist the three deliverables (FDD Markdown, test case workbook, data mapping
   workbook) plus the intermediate JSON artifacts.
2. Keep the ``Run.artifacts`` list in step with what is actually on disk, so the API
   and the UI never have to guess.

Workbook generation is deliberately **server-side and deterministic**: openpyxl builds
the file from validated rows rather than a model emitting raw XLSX. That keeps the
artifact schema stable and makes the output reproducible from the JSON alone.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.domain.models import Artifact, ArtifactKind, ArtifactState, now_iso

logger = logging.getLogger(__name__)

#: Column definitions are the contract for each workbook. Changing a key here is a
#: contract change (see CONTRIBUTING.md §2).
#:
#: TODO(demo): the column layout is ours; the customer's templates win the moment they arrive
#: (docs/DEMO.md §7 gap 7).
TEST_CASE_COLUMNS: list[tuple[str, str, int]] = [
    ("case_id", "Case ID", 14),
    ("title", "Title", 52),
    ("type", "Type", 12),
    ("priority", "Priority", 10),
    ("precondition", "Precondition", 40),
    ("steps", "Steps", 60),
    ("expected", "Expected result", 52),
    ("rule_id", "Rule ID", 18),
    ("slice_id", "Slice ID", 14),
    ("source_file", "Source file", 40),
]

DATA_MAPPING_COLUMNS: list[tuple[str, str, int]] = [
    ("mapping_id", "Mapping ID", 14),
    ("interface", "Interface", 24),
    ("program", "Program", 16),
    ("source_field", "Source field", 32),
    ("source_type", "Source type", 18),
    ("target_field", "Target field", 32),
    ("target_type", "Target type", 18),
    ("transform", "Transform", 24),
    ("copybook", "Copybook / file", 34),
    ("remarks", "Remarks", 40),
]

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)


def _write_sheet(ws, columns: list[tuple[str, str, int]], rows: list[dict[str, Any]]) -> None:
    """Write a header row plus data rows, with widths, wrapping and a frozen pane."""
    for index, (_, title, width) in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=index, value=title)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")
        ws.column_dimensions[get_column_letter(index)].width = width
    ws.freeze_panes = "A2"

    for row_index, row in enumerate(rows, start=2):
        for col_index, (key, _, _) in enumerate(columns, start=1):
            cell = ws.cell(row=row_index, column=col_index, value=_cell_value(row.get(key, "")))
            cell.alignment = Alignment(vertical="top", wrap_text=True)


def _cell_value(value: Any) -> Any:
    """Coerce a generated value into something openpyxl can store.

    Real models do not honour a prose schema literally: a field documented as
    "steps: newline-separated string" routinely comes back as a JSON array of strings.
    Handing that straight to openpyxl raises ``Cannot convert [...] to Excel`` and fails the
    whole deliverable — which is how a run that had generated perfectly good test cases
    reported the artifact as failed while the other two succeeded.

    Lists become newline-joined text (readable inside a cell), dicts become compact JSON,
    and scalars pass through. Putting the coercion here rather than relying on the prompt
    means a model that formats differently loses nothing.
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list | tuple):
        return "\n".join(str(_cell_value(item)) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


class ArtifactStore:
    """Filesystem layout and writers for one run's deliverables."""

    def __init__(self, artifacts_dir: Path) -> None:
        self.dir = artifacts_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    def path(self, filename: str) -> Path:
        return self.dir / filename

    def write_text(self, filename: str, content: str) -> Path:
        target = self.path(filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def write_json(self, filename: str, payload: Any) -> Path:
        return self.write_text(filename, json.dumps(payload, ensure_ascii=False, indent=2))

    def read_json(self, filename: str) -> Any:
        target = self.path(filename)
        if not target.exists():
            return None
        return json.loads(target.read_text(encoding="utf-8"))

    # --------------------------------------------------------------- workbook
    def write_workbook(
        self,
        filename: str,
        sheet_title: str,
        columns: list[tuple[str, str, int]],
        rows: list[dict[str, Any]],
        info: dict[str, Any] | None = None,
    ) -> tuple[Path, int]:
        """Build a workbook with a data sheet and a run-info sheet.

        The info sheet exists so a downloaded file is self-describing: a reviewer
        opening it months later can see which revision and run produced it.
        """
        wb = Workbook()
        ws = wb.active
        ws.title = sheet_title[:31]  # Excel caps sheet names at 31 characters
        _write_sheet(ws, columns, rows)

        meta = wb.create_sheet("Run info")
        meta.column_dimensions["A"].width = 22
        meta.column_dimensions["B"].width = 70
        meta.append(["Field", "Value"])
        for cell in meta[1]:
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
        for key, value in (info or {}).items():
            meta.append([key, "" if value is None else str(value)])

        target = self.path(filename)
        wb.save(target)
        return target, len(rows)

    # ------------------------------------------------------------------ state
    def mark(
        self,
        artifacts: list[Artifact],
        kind: ArtifactKind,
        *,
        state: ArtifactState | None = None,
        filename: str | None = None,
        summary: str = "",
        error: str | None = None,
    ) -> list[Artifact]:
        """Update one artifact entry in place and return the list."""
        for artifact in artifacts:
            if artifact.kind != kind:
                continue
            if state is not None:
                artifact.state = state
            if filename is not None:
                artifact.filename = filename
                target = self.path(filename)
                artifact.size_bytes = target.stat().st_size if target.exists() else 0
            if summary:
                artifact.summary = summary
            artifact.error = error
            artifact.updated_at = now_iso()
            break
        return artifacts

    def verify(self, filename: str) -> tuple[bool, str]:
        """Cheap sanity check that a deliverable is non-empty and parseable."""
        target = self.path(filename)
        if not target.exists():
            return False, f"missing file: {filename}"
        if target.stat().st_size == 0:
            return False, f"empty file: {filename}"
        if filename.endswith(".xlsx"):
            try:
                Workbook(target)
            except Exception as exc:
                return False, f"workbook unreadable: {exc}"
        if filename.endswith(".json"):
            try:
                json.loads(target.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                return False, f"invalid JSON: {exc}"
        return True, "ok"

    def listing(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in sorted(self.dir.rglob("*")):
            if path.is_file():
                out.append(
                    {
                        "name": path.relative_to(self.dir).as_posix(),
                        "size_bytes": path.stat().st_size,
                    }
                )
        return out


def extract_json_rows(content: str, key: str = "rows") -> list[dict[str, Any]]:
    """Pull a row list out of a model response.

    Models wrap JSON in prose or a fenced block, so accept both. Returns an empty
    list rather than raising: callers decide whether that is fatal, and a partial
    deliverable is more useful than a crashed run.
    """
    text = content.strip()
    if "```" in text:
        blocks = text.split("```")
        for block in blocks:
            candidate = block.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{") or candidate.startswith("["):
                rows = _rows_from_json(candidate, key)
                if rows:
                    return rows
    return _rows_from_json(text, key)


def _rows_from_json(text: str, key: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # Fall back to the outermost object in the text.
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return []
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []
