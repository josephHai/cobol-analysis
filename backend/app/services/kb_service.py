"""Knowledge base: chunking, storage and retrieval over generated artifacts.

Two layers, deliberately separated:

* **Chunking** — product-specific, stays here. Test cases and data mappings become
  one document per row (they are looked up by field/case, not by prose); the FDD is
  split by heading; JSON artifacts split by rule or slice. Each chunk carries a
  ``locator`` that lets the UI jump back to the exact source.
* **Retrieval** — behind :class:`Retriever`. The demo ships a deterministic lexical
  retriever with no external service and no model call, which is what makes the
  knowledge-base flow demonstrable offline. The intranet deployment swaps in the real
  vector store by implementing the same protocol; nothing else changes.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Protocol

from app.domain.models import (
    ArtifactKind,
    KbChunk,
    KbCitation,
    KbQueryRequest,
    KbQueryResponse,
    Run,
)
from app.infra.db import Database
from app.services.artifacts import ArtifactStore

logger = logging.getLogger(__name__)

#: Markdown headings are the natural chunk boundary for prose deliverables.
RE_HEADING = re.compile(r"^(#{1,4})\s+(.*)$", re.M)
RE_TOKEN = re.compile(r"[a-z0-9_\-]{2,}", re.I)
STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "are",
    "was",
    "were",
    "has",
    "have",
    "not",
    "but",
    "you",
    "your",
    "its",
    "into",
    "than",
    "then",
    "when",
    "which",
    "what",
    "how",
    "why",
    "can",
    "will",
    "shall",
    "may",
    "must",
    "each",
    "per",
    "via",
    "code",
    "file",
    "files",
    "data",
}


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in RE_TOKEN.findall(text) if t.lower() not in STOPWORDS]


# --------------------------------------------------------------------- chunking
def chunk_fdd(run_id: str, repo_key: str, commit_sha: str | None, markdown: str) -> list[KbChunk]:
    """Split the FDD on headings, keeping the heading path as context."""
    matches = list(RE_HEADING.finditer(markdown))
    if not matches:
        return [_chunk(run_id, repo_key, commit_sha, ArtifactKind.FDD, "FDD", markdown, "")]

    chunks: list[KbChunk] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        body = markdown[start:end].strip()
        if len(body) < 40:
            continue
        title = match.group(2).strip()
        anchor = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        chunks.append(
            _chunk(run_id, repo_key, commit_sha, ArtifactKind.FDD, title, body, f"# {anchor}")
        )
    return chunks or [_chunk(run_id, repo_key, commit_sha, ArtifactKind.FDD, "FDD", markdown, "")]


def chunk_rows(
    run_id: str,
    repo_key: str,
    commit_sha: str | None,
    kind: ArtifactKind,
    sheet: str,
    rows: list[dict[str, Any]],
    title_key: str,
) -> list[KbChunk]:
    """One chunk per row — field-level and case-level questions need that granularity."""
    chunks: list[KbChunk] = []
    for index, row in enumerate(rows, start=1):
        text = " | ".join(f"{k}={v}" for k, v in row.items() if v not in ("", None))
        if not text.strip():
            continue
        chunks.append(
            _chunk(
                run_id,
                repo_key,
                commit_sha,
                kind,
                str(row.get(title_key) or f"{kind} row {index}"),
                text,
                f"{sheet}!row{index + 1}",  # +1 accounts for the header row
            )
        )
    return chunks


def chunk_analysis(
    run_id: str, repo_key: str, commit_sha: str | None, analysis: dict[str, Any]
) -> list[KbChunk]:
    """Split the intermediate analysis JSON by rule and by slice."""
    chunks: list[KbChunk] = []
    for rule in (analysis.get("business_rules") or {}).get("rules", []):
        chunks.append(
            _chunk(
                run_id,
                repo_key,
                commit_sha,
                ArtifactKind.BUSINESS_RULES,
                f"{rule.get('rule_id')} {rule.get('description', '')[:60]}",
                f"rule_id={rule.get('rule_id')} type={rule.get('type')} "
                f"condition={rule.get('condition')} "
                f"evidence={(rule.get('evidence') or {}).get('file')} "
                f"statement={(rule.get('evidence') or {}).get('statement')}",
                f"$.business_rules.rules[{rule.get('rule_id')}]",
            )
        )
    for sl in (analysis.get("code_slice") or {}).get("slices", []):
        chunks.append(
            _chunk(
                run_id,
                repo_key,
                commit_sha,
                ArtifactKind.CODE_SLICE,
                f"{sl.get('slice_id')} {sl.get('file')}",
                f"slice={sl.get('slice_id')} program={sl.get('program')} "
                f"file={sl.get('file')} entry={sl.get('entry_paragraph')} "
                f"paragraphs={','.join(sl.get('paragraphs') or [])}",
                f"$.code_slice.slices[{sl.get('slice_id')}]",
            )
        )
    return chunks


def _chunk(
    run_id: str,
    repo_key: str,
    commit_sha: str | None,
    kind: ArtifactKind,
    title: str,
    text: str,
    locator: str,
) -> KbChunk:
    # ``hash()`` is randomised per process for strings, so it would hand the same chunk a
    # different id after a restart. sha1 over the same fields is stable, which is what makes a
    # chunk id comparable between two runs of the same analysis.
    digest = hashlib.sha1(
        f"{run_id}|{kind}|{locator}|{title}".encode(), usedforsecurity=False
    ).hexdigest()[:12]
    return KbChunk(
        id=f"{run_id}:{kind}:{digest}",
        run_id=run_id,
        repo_key=repo_key,
        commit_sha=commit_sha,
        kind=kind,
        title=title[:200],
        text=text[:4000],
        locator=locator,
        metadata={"kind": str(kind)},
    )


# -------------------------------------------------------------------- retrieval
class Retriever(Protocol):
    """Swap-in point for the real vector store."""

    name: str

    def search(
        self, chunks: list[KbChunk], question: str, top_k: int
    ) -> list[tuple[KbChunk, float]]: ...


class LexicalRetriever:
    """Deterministic BM25-flavoured retrieval over an in-memory chunk set.

    TODO(demo): this stands in for the intranet vector store (docs/DEMO.md §7 gap 4); implement
    :class:`Retriever` against it and nothing else changes.

    Chosen for the demo because it needs no model, no network and no index build,
    and behaves predictably during a live presentation. It is a real retriever —
    not a stub — so answer quality reflects retrieval quality honestly.
    """

    name = "lexical"

    def search(
        self, chunks: list[KbChunk], question: str, top_k: int
    ) -> list[tuple[KbChunk, float]]:
        query = Counter(tokenize(question))
        if not query or not chunks:
            return []

        docs = [Counter(tokenize(f"{c.title} {c.text}")) for c in chunks]
        lengths = [sum(d.values()) or 1 for d in docs]
        avg_len = sum(lengths) / len(lengths)
        doc_freq: Counter[str] = Counter()
        for doc in docs:
            doc_freq.update(doc.keys())

        total = len(docs)
        k1, b = 1.5, 0.75
        scored: list[tuple[KbChunk, float]] = []
        for chunk, doc, length in zip(chunks, docs, lengths, strict=True):
            score = 0.0
            for term, q_count in query.items():
                freq = doc.get(term, 0)
                if not freq:
                    continue
                idf = math.log(1 + (total - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5))
                denom = freq + k1 * (1 - b + b * length / avg_len)
                score += idf * (freq * (k1 + 1)) / denom * (1 + math.log(q_count))
            if score > 0:
                scored.append((chunk, score))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]


class KnowledgeBase:
    """Chunk storage plus query orchestration.

    Embedding generation is intentionally absent: when the real store lands, chunks
    are embedded on write and this class keeps its interface.
    """

    def __init__(self, db: Database, retriever: Retriever | None = None) -> None:
        self.db = db
        self.retriever: Retriever = retriever or LexicalRetriever()

    # ------------------------------------------------------------------ ingest
    def index_run(self, run: Run, analysis: dict[str, Any] | None, artifacts_dir: Path) -> int:
        """(Re)index every artifact of a run. Idempotent: replaces prior chunks."""
        store = ArtifactStore(artifacts_dir)
        chunks: list[KbChunk] = []

        fdd = artifacts_dir / "fdd.md"
        if fdd.exists():
            chunks += chunk_fdd(run.id, run.repo_key, run.commit_sha, fdd.read_text("utf-8"))

        test_cases = store.read_json("test_cases.json")
        if isinstance(test_cases, dict) and isinstance(test_cases.get("rows"), list):
            chunks += chunk_rows(
                run.id,
                run.repo_key,
                run.commit_sha,
                ArtifactKind.TEST_CASE,
                "Test cases",
                test_cases["rows"],
                "case_id",
            )

        mapping = store.read_json("data_mapping.json")
        if isinstance(mapping, dict) and isinstance(mapping.get("rows"), list):
            chunks += chunk_rows(
                run.id,
                run.repo_key,
                run.commit_sha,
                ArtifactKind.DATA_MAPPING,
                "Data mapping",
                mapping["rows"],
                "mapping_id",
            )

        if analysis:
            chunks += chunk_analysis(run.id, run.repo_key, run.commit_sha, analysis)

        written = self.db.replace_chunks(run.id, chunks)
        logger.info("kb: indexed %d chunks for run %s", written, run.id)
        return written

    # ------------------------------------------------------------------- query
    def query(self, request: KbQueryRequest) -> KbQueryResponse:
        chunks = self.db.all_chunks(repo_key=request.repo_key)
        if request.run_id:
            chunks = [c for c in chunks if c.run_id == request.run_id]

        hits = self.retriever.search(chunks, request.question, request.top_k)
        if not hits:
            return KbQueryResponse(
                answer=(
                    "No supporting evidence was found in the knowledge base. "
                    "Try naming a program, field, rule ID, or interface."
                ),
                citations=[],
                provider=self.retriever.name,
            )

        citations = [
            KbCitation(
                run_id=chunk.run_id,
                repo_key=chunk.repo_key,
                commit_sha=chunk.commit_sha,
                kind=chunk.kind,
                title=chunk.title,
                locator=chunk.locator,
                snippet=_snippet(chunk.text, request.question),
            )
            for chunk, _score in hits
        ]
        return KbQueryResponse(
            answer=_compose_answer(request.question, citations),
            citations=citations,
            provider=self.retriever.name,
        )


def _snippet(text: str, question: str, width: int = 260) -> str:
    """Centre the snippet on the best-matching term so the citation is inspectable."""
    terms = tokenize(question)
    lowered = text.lower()
    position = -1
    for term in terms:
        position = lowered.find(term)
        if position != -1:
            break
    if position == -1:
        return text[:width] + ("…" if len(text) > width else "")
    start = max(0, position - width // 3)
    snippet = text[start : start + width]
    return ("…" if start else "") + snippet + ("…" if start + width < len(text) else "")


def _compose_answer(question: str, citations: list[KbCitation]) -> str:
    """Grounded answer assembled from retrieved chunks.

    TODO(demo): assembled, not generated (docs/DEMO.md §7 gap 5); this becomes prompt assembly
    when a model is put in the loop, and the citations stay as they are.

    This is a retrieval summary, not a generated narrative: with no model in the loop
    for the demo, the honest thing is to present the evidence and say so. When the
    gateway is wired in, this function becomes the prompt-assembly step and the
    citations stay exactly as they are.
    """
    lines = [f'Evidence found for: "{question}"', ""]
    for index, citation in enumerate(citations, start=1):
        lines.append(f"{index}. [{citation.kind}] {citation.title}")
        if citation.locator:
            lines.append(f"   location: {citation.locator}")
        lines.append(f"   {citation.snippet}")
        lines.append("")
    lines.append(
        "Every statement above comes from a generated artifact of the cited run; "
        "follow the location to inspect the source."
    )
    return "\n".join(lines)
