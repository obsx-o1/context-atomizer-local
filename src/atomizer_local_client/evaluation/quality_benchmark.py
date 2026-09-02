"""Boring, deterministic retrieval characterization with no model-as-judge step."""

from __future__ import annotations

import json
import argparse
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

from atomizer_local_client.chat.contracts import ChatEvent, Host, Role
from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.derived_state.cycle import run_derived_state_cycle
from atomizer_local_client.library.document_registry import (
    elect_file_source,
    sync_elected_source,
)
from atomizer_local_client.memory_access.query_service import LibraryQueryService


@dataclass(frozen=True, slots=True)
class _Case:
    name: str
    query: str
    project_id: str
    expected_source_ids: tuple[str, ...]
    forbidden_source_ids: tuple[str, ...] = ()


def _event(
    identity: str,
    *,
    project: str,
    chat: str,
    content: str,
    captured_at: str,
) -> ChatEvent:
    return ChatEvent(
        event_id=identity,
        host=Host.CODEX,
        host_project_reference=project,
        host_chat_reference=chat,
        host_turn_reference=identity,
        role=Role.USER,
        content=content,
        captured_at=captured_at,
        project_display_name=project.title(),
        chat_display_name=chat,
    )


def _state_for_results(database_path: Path, evidence_ids: list[str]) -> dict[str, str]:
    if not evidence_ids:
        return {}
    placeholders = ",".join("?" for _ in evidence_ids)
    connection = sqlite3.connect(database_path)
    try:
        return {
            str(row[0]): str(row[1])
            for row in connection.execute(
                f"SELECT evidence_id,state FROM temporal_evidence_state "
                f"WHERE evidence_id IN ({placeholders})",
                evidence_ids,
            )
        }
    finally:
        connection.close()


def run_quality_benchmark(root: Path | None = None) -> dict[str, Any]:
    """Build a disposable two-project Library and return inspectable metrics."""

    temporary: tempfile.TemporaryDirectory[str] | None = None
    if root is None:
        temporary = tempfile.TemporaryDirectory()
        fixture_root = Path(temporary.name)
    else:
        fixture_root = Path(root)
        fixture_root.mkdir(parents=True, exist_ok=True)
    database_path = fixture_root / "history.sqlite3"
    try:
        exact = ingest_chat_event(
            database_path,
            _event(
                "atlas-codename",
                project="atlas",
                chat="atlas-history",
                content="Project Atlas release codename is Amber.",
                captured_at="2026-01-01T00:00:00+00:00",
            ),
        )
        decision = ingest_chat_event(
            database_path,
            _event(
                "atlas-decision",
                project="atlas",
                chat="atlas-decisions",
                content="Decision: Project Atlas uses SQLite.",
                captured_at="2026-02-01T00:00:00+00:00",
            ),
        )
        mobile = ingest_chat_event(
            database_path,
            _event(
                "atlas-mobile",
                project="atlas",
                chat="atlas-mobile",
                content="Project Atlas Mobile uses Realm.",
                captured_at="2026-03-01T00:00:00+00:00",
            ),
        )
        failed = ingest_chat_event(
            database_path,
            _event(
                "atlas-failed-approach",
                project="atlas",
                chat="atlas-experiments",
                content=(
                    "Atlas WAL-disabled SQLite failed because readers blocked writers."
                ),
                captured_at="2026-02-15T00:00:00+00:00",
            ),
        )
        recent = ingest_chat_event(
            database_path,
            _event(
                "atlas-recent",
                project="atlas",
                chat="atlas-current",
                content="Project Atlas current release train is Cedar.",
                captured_at="2026-08-01T00:00:00+00:00",
            ),
        )
        borealis = ingest_chat_event(
            database_path,
            _event(
                "borealis-codename",
                project="borealis",
                chat="borealis-history",
                content="Project Borealis release codename is Amber.",
                captured_at="2026-04-01T00:00:00+00:00",
            ),
        )

        status_path = fixture_root / "atlas-status.md"
        status_path.write_text("Project Atlas status is red.", encoding="utf-8")
        status_source = elect_file_source(database_path, exact.project_id, status_path)
        run_derived_state_cycle(database_path)
        status_path.write_text("Project Atlas status is green.", encoding="utf-8")
        sync_elected_source(database_path, status_source.source_id)

        limit_a = fixture_root / "atlas-limit-a.md"
        limit_b = fixture_root / "atlas-limit-b.md"
        limit_a.write_text("Project Atlas limit = 10", encoding="utf-8")
        limit_b.write_text("Project Atlas limit = 20", encoding="utf-8")
        elect_file_source(database_path, exact.project_id, limit_a)
        elect_file_source(database_path, exact.project_id, limit_b)
        run_derived_state_cycle(database_path)

        connection = sqlite3.connect(database_path)
        try:
            current_status = str(
                connection.execute(
                    "SELECT document_id FROM documents WHERE local_source_reference = ?",
                    (str(status_path.resolve()),),
                ).fetchone()[0]
            )
            disputed_sources = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT DISTINCT e.source_id FROM claim_evidence e
                    JOIN temporal_evidence_state t ON t.evidence_id=e.evidence_id
                    WHERE t.state='disputed'
                    """
                )
            }
        finally:
            connection.close()

        cases = (
            _Case("exact_historical_fact", "release codename Amber", exact.project_id, (exact.message_id,), (borealis.message_id,)),
            _Case("decision", "uses SQLite", decision.project_id, (decision.message_id,)),
            _Case(
                "failed_approach",
                "WAL disabled failed readers blocked writers",
                failed.project_id,
                (failed.message_id,),
            ),
            _Case("current_over_superseded", "status green", exact.project_id, (current_status,)),
            _Case("similarly_named_entity", "Atlas Mobile Realm", mobile.project_id, (mobile.message_id,)),
            _Case("recent_fact", "release train Cedar", recent.project_id, (recent.message_id,)),
            _Case("contradictory_fact", "Atlas limit", exact.project_id, tuple(sorted(disputed_sources))),
        )
        service = LibraryQueryService(database_path)
        hits = 0
        reciprocal_ranks: list[float] = []
        returned = 0
        provenance_complete = 0
        stale = 0
        bleed = 0
        output_characters = 0
        latencies_ms: list[float] = []
        contradictory_expected = 0
        contradictory_returned = 0
        case_results: list[dict[str, object]] = []
        for case in cases:
            started = time.perf_counter()
            result = service.search_library(case.query, case.project_id, 8)
            elapsed_ms = (time.perf_counter() - started) * 1000
            latencies_ms.append(elapsed_ms)
            items = list(result["items"])
            source_ids = [str(item["source_id"]) for item in items]
            evidence_ids = [str(item["evidence_id"]) for item in items]
            states = _state_for_results(database_path, evidence_ids)
            matched = [source for source in case.expected_source_ids if source in source_ids]
            if matched:
                hits += 1
                reciprocal_ranks.append(
                    1.0 / min(source_ids.index(source) + 1 for source in matched)
                )
            else:
                reciprocal_ranks.append(0.0)
            returned += len(items)
            provenance_complete += sum(
                all(item.get(field) is not None for field in ("source_type", "source_id", "project_id", "timestamp"))
                for item in items
            )
            stale += sum(states.get(identity) == "superseded" for identity in evidence_ids)
            bleed += sum(source in source_ids for source in case.forbidden_source_ids)
            output_characters += len(json.dumps(result, ensure_ascii=False))
            if case.name == "contradictory_fact":
                contradictory_expected = len(case.expected_source_ids)
                contradictory_returned = len(matched)
            case_results.append(
                {
                    "name": case.name,
                    "latency_ms": round(elapsed_ms, 3),
                    "result_count": len(items),
                    "expected_found": len(matched),
                    "expected_total": len(case.expected_source_ids),
                }
            )

        recent_result = service.recent_library_context(exact.project_id, 8)
        recent_timestamps = [
            str(item["timestamp"]) for item in recent_result["items"]
        ]
        recent_order_is_descending = recent_timestamps == sorted(
            recent_timestamps, reverse=True
        )
        return {
            "schema_version": "context-atomizer-library-quality-v1",
            "fixture": {
                "projects": 2,
                "query_cases": len(cases),
                "model_as_judge": False,
                "retrieval_algorithms_changed": False,
            },
            "metrics": {
                "recall_at_8": hits / len(cases),
                "mrr": sum(reciprocal_ranks) / len(reciprocal_ranks),
                "invalid_stale_evidence_rate": stale / returned if returned else 0.0,
                "superseded_current_truth_error_rate": stale / returned if returned else 0.0,
                "contradiction_awareness_rate": (
                    contradictory_returned / contradictory_expected
                    if contradictory_expected
                    else 0.0
                ),
                "project_scope_bleed_rate": bleed / returned if returned else 0.0,
                "provenance_coverage": provenance_complete / returned if returned else 1.0,
                "recent_order_is_descending": recent_order_is_descending,
                "latency_ms_median": round(median(latencies_ms), 3),
                "latency_ms_max": round(max(latencies_ms), 3),
                "result_count": returned,
                "output_characters": output_characters,
                "approximate_output_tokens": (output_characters + 3) // 4,
            },
            "cases": case_results,
        }
    finally:
        if temporary is not None:
            temporary.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build and characterize a disposable local Library"
    )
    parser.add_argument("--root", type=Path)
    arguments = parser.parse_args()
    print(
        json.dumps(
            run_quality_benchmark(arguments.root), indent=2, sort_keys=True
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
