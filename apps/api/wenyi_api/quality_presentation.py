"""Read-only, paginated quality artifacts with server-verified snapshot identities."""

from __future__ import annotations

import re
from typing import Any

from wenyi_core.config import Config
from wenyi_core.quality.context import build_context
from wenyi_core.quality.units import build_quality_units

_UNIT_ID = re.compile(r"ch\d+:text\d+:seg\d+")


def quality_records(storage, rid: str, config: Config, *, view: str = "formal") -> list[dict]:
    """Read existing scores only; page refreshes never create judgments or candidates."""
    prefix = f"reviews/{rid}/quality"
    manifest = storage.read_artifact(f"{prefix}/manifest.json") or {}
    if not manifest.get("units"):
        return []
    book = storage.load_manifest()
    config = config.model_copy(deep=True)
    config.source_lang = book.get("source_lang") or config.source_lang
    config.target_lang = book.get("target_lang") or config.target_lang
    chapters = [storage.load_chapter(row["index"]) for row in book.get("chapters", [])]
    units = build_quality_units(chapters)
    current = {unit.unit_id: unit for unit in units}
    terms = storage.all_terms()
    analysis = storage.load_analysis()
    index = storage.read_artifact(f"reviews/{rid}/autofix/index.json") or {}
    locations = index.get("locations", [])
    rows = []
    for identity in manifest.get("units", []):
        if not isinstance(identity, dict):
            continue
        uid = identity.get("unit_id", "")
        if not _UNIT_ID.fullmatch(uid):
            continue
        baseline = storage.read_artifact(f"{prefix}/units/{uid}/baseline.json") or {}
        row = storage.read_artifact(f"{prefix}/final_scores/{view}/{uid}.json")
        row = dict(row or baseline)
        if not row:
            row = {**identity, "status": "pending", "dimensions": {}}
        row.setdefault("unit_id", uid)
        row.setdefault("chapter_index", identity.get("chapter_index", 0))
        row.setdefault("members", identity.get("members", []))
        row["text_scope"] = view
        row["decision"] = storage.read_artifact(f"{prefix}/units/{uid}/decision.json") or row.get(
            "decision", {}
        )
        raw_decision = row["decision"]
        row["decision"] = {
            key: value
            for key, value in raw_decision.items()
            if key
            in {
                "action",
                "accepted",
                "reason_codes",
                "selected_candidate_id",
                "candidate_id",
                "origin",
                "decision_ref",
                "calibration_status",
            }
        }
        if "accepted" in row["decision"]:
            row["decision"]["action"] = "accepted" if row["decision"]["accepted"] else "rejected"
        unit = current.get(uid)
        stale = True
        if unit is not None and row.get("source_hash") == unit.source_hash:
            context = build_context(unit, units, config, terms, analysis)
            stale = (row.get("target_hash"), row.get("context_hash")) != (
                unit.target_hash,
                context.context_hash,
            )
            if stale and view == "shadow":
                base_hash = row.get("formal_base_hash", baseline.get("target_hash"))
                base_context = row.get("formal_base_context_hash", baseline.get("context_hash"))
                stale = (base_hash, base_context) != (unit.target_hash, context.context_hash)
        row["stale"] = stale
        row["publication_status"] = _publication_status(row, locations, unit)
        row["calibration_status"] = "uncalibrated"
        scores = [
            value.get("normalized")
            for value in row.get("dimensions", {}).values()
            if isinstance(value, dict)
        ]
        values = [
            score
            for score in scores
            if isinstance(score, (float, int)) and not isinstance(score, bool)
        ]
        row["minimum_score"] = min(values) if values else None
        rows.append(row)
    return rows


def _publication_status(row: dict, locations: list[dict], unit=None) -> str:
    members = {
        (row.get("chapter_index"), member.get("text_index")) for member in row.get("members", [])
    }
    matching = [item for item in locations if (item.get("chapter"), item.get("index")) in members]
    if not matching:
        return "not_published"
    applied = {
        (item.get("chapter"), item.get("index"))
        for item in matching
        if item.get("status") in {"applied", "no_net_change"}
    }
    if applied == members and any(item.get("status") == "applied" for item in matching):
        actual = dict(zip(unit.text_indices, unit.target_parts)) if unit is not None else {}
        if any(
            item.get("index") not in actual or actual[item["index"]] != item.get("target")
            for item in matching
        ):
            return "changed"
        return "published"
    if applied == members:
        return "not_published"
    if applied:
        return "partial"
    return "failed" if any(item.get("status") == "failed" for item in matching) else "not_published"


def candidate_details(storage, rid: str, uid: str) -> list[dict[str, Any]]:
    """Expose candidate text and provenance, never stored request prompts or credentials."""
    prefix = f"reviews/{rid}/quality/units/{uid}/candidates/"
    fields = {
        "candidate_id",
        "target_parts",
        "strategy",
        "operation",
        "model",
        "model_identity_kind",
        "provider",
        "status",
        "reason_codes",
        "target_hash",
        "accepted",
        "comparisons",
    }
    results = []
    for key in storage.list_artifacts(prefix):
        if not key.endswith(".json"):
            continue
        value = storage.read_artifact(key)
        if isinstance(value, dict):
            results.append({key: item for key, item in value.items() if key in fields})
    return results
