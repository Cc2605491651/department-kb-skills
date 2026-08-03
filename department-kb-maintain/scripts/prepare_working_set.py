#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import maintain_common as common


RESET_FIELDS = {
    "parse_status": "待解析",
    "first_attempt": "",
    "second_attempt": "",
    "last_error": "",
    "source_hash": "",
    "extracted_hash": "",
    "extracted_chars": "",
    "processing": "待处理",
    "status": "候选",
    "delivery_status": "",
    "delivery_error": "",
    "failure_stage": "",
    "failure_category": "",
    "attempt_count": "",
    "last_attempt_at": "",
    "download_bytes": "",
    "file_signature": "",
    "http_status": "",
}

# Values enriched through deterministic secondary lookups may be absent from a
# later tree scan. Preserve the last known value instead of replacing it with an
# empty observation; new sources are still enriched by the owners stage.
PRESERVE_WHEN_CURRENT_EMPTY = {
    "creator_uid", "creator_name", "owner", "permission_snapshot", "permission_hash",
}


def prepare(job: Path) -> list[dict]:
    job = job.resolve()
    paths = common.state_paths(job)
    observed = common.load_json(paths["latest"], {})
    plan = common.load_json(paths["plan"], {})
    old_rows = common.load_json(job / "01-inventory" / "raw-manifest.json", [])
    if not isinstance(observed, dict) or not isinstance(observed.get("documents"), list):
        raise RuntimeError("缺少有效观察快照")
    if not isinstance(plan, dict) or plan.get("snapshot_id") != observed.get("snapshot_id"):
        raise RuntimeError("变化计划与最新观察快照不一致")
    if not isinstance(old_rows, list):
        raise RuntimeError("当前工作清单不是数组")
    old_by_id = {str(row.get("source_id")): row for row in old_rows if isinstance(row, dict) and row.get("source_id")}
    applied = common.load_json(paths["applied"], {}) or {}
    applied_by_id = applied.get("documents") if isinstance(applied.get("documents"), dict) else {}
    extract_ids = set((plan.get("actions") or {}).get("extract_source_ids") or [])
    change_by_id = {
        str(row.get("source_id")): row
        for row in plan.get("changes") or [] if isinstance(row, dict) and row.get("source_id")
    }
    merged: list[dict] = []
    for current in observed["documents"]:
        if not isinstance(current, dict) or not current.get("source_id"):
            continue
        source_id = str(current["source_id"])
        previous = old_by_id.get(source_id)
        if not isinstance(previous, dict):
            previous = applied_by_id.get(source_id) if isinstance(applied_by_id.get(source_id), dict) else {}
        row = dict(previous)
        for field in common.SOURCE_FIELDS:
            value = current.get(field, "")
            if field in PRESERVE_WHEN_CURRENT_EMPTY and not value:
                continue
            row[field] = value
        for key, value in current.items():
            row.setdefault(key, value)
        row["snapshot_status"] = "本次增量扫描已发现"
        row["incremental_change_type"] = (change_by_id.get(source_id) or {}).get("change_type", "unchanged")
        if source_id in extract_ids:
            row.update(RESET_FIELDS)
        else:
            row.setdefault("parse_status", "待解析")
            row.setdefault("processing", "待处理")
            row.setdefault("status", "候选")
        merged.append(row)

    missing = [
        row for row in plan.get("changes") or []
        if isinstance(row, dict) and row.get("change_type") in {"suspected_missing", "source_orphan"}
    ]
    orphan_rows: list[dict] = []
    for item in missing:
        source_id = str(item.get("source_id") or "")
        source = old_by_id.get(source_id) or applied_by_id.get(source_id) or {}
        orphan = {**source, **item}
        orphan_rows.append(orphan)
        retained = dict(source)
        retained["snapshot_status"] = "原文失联待负责人确认"
        retained["incremental_change_type"] = item.get("change_type", "suspected_missing")
        retained["source_presence"] = "missing_pending_confirmation"
        if retained.get("source_id"):
            merged.append(retained)
    backup = paths["root"] / "backups" / str(plan.get("run_id") or common.run_id())
    common.write_json(backup / "raw-manifest.before.json", old_rows)
    common.write_json(backup / "change-plan.json", plan)
    common.write_json(paths["orphans"], orphan_rows)
    fields: list[str] = []
    for row in [*old_rows, *merged]:
        for key in row:
            if key not in fields:
                fields.append(key)
    common.write_json(job / "01-inventory" / "raw-manifest.json", sorted(merged, key=lambda row: str(row.get("source_path") or "")))
    common.write_csv(job / "01-inventory" / "raw-manifest.csv", sorted(merged, key=lambda row: str(row.get("source_path") or "")), fields)
    common.append_jsonl(paths["root"] / "maintenance-history.jsonl", {
        "event": "working_set_prepared", "at": common.now_iso(),
        "run_id": plan.get("run_id", ""), "active": len(merged), "source_orphans": len(orphan_rows),
    })
    print(f"WORKING_SET_OK active={len(merged)} source_orphans={len(orphan_rows)} extract={len(extract_ids)}")
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge observed source metadata into the existing distillation working manifest.")
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.job)


if __name__ == "__main__":
    main()
