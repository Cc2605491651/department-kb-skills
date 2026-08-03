#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import maintain_common as common
from initialize_baseline import initialize


PRIORITY = {
    "source_orphan": 100,
    "suspected_missing": 90,
    "new": 80,
    "restored": 75,
    "content_check": 70,
    "permission_changed": 60,
    "moved": 50,
    "renamed": 40,
    "metadata_changed": 30,
    "unchanged": 0,
}


def primary_type(types: list[str]) -> str:
    return max(types or ["unchanged"], key=lambda value: PRIORITY.get(value, 1))


def build(job: Path, *, missing_confirm_runs: int = 2, force_hash_audit: bool = False) -> dict:
    job = job.resolve()
    paths = common.state_paths(job)
    if not paths["applied"].exists():
        initialize(job)
    observed = common.load_json(paths["latest"], {})
    applied = common.load_json(paths["applied"], {})
    if not isinstance(observed, dict) or not isinstance(observed.get("documents"), list):
        raise RuntimeError("缺少latest-observed.json；请先执行scan")
    if not isinstance(applied, dict) or not isinstance(applied.get("documents"), dict):
        raise RuntimeError("成功基线无效；请重新执行baseline")
    observation = common.load_json(paths["observation"], {}) or {}
    previous_observation = observation.get("documents") if isinstance(observation, dict) else {}
    if not isinstance(previous_observation, dict):
        previous_observation = {}
    same_observation_snapshot = str(observation.get("last_snapshot_id") or "") == str(observed.get("snapshot_id") or "")
    current_by_id = {
        str(row.get("source_id")): row
        for row in observed["documents"] if isinstance(row, dict) and row.get("source_id")
    }
    applied_by_id = applied["documents"]
    changes: list[dict] = []
    next_observation: dict[str, dict] = {}
    extract_ids: set[str] = set()
    semantic_ids: set[str] = set()
    affected_ids: set[str] = set()
    processing_ids: set[str] = set()

    for source_id, current in current_by_id.items():
        old = applied_by_id.get(source_id)
        prior_seen = previous_observation.get(source_id) if isinstance(previous_observation.get(source_id), dict) else {}
        next_observation[source_id] = {
            "last_seen_at": observed.get("observed_at", common.now_iso()),
            "missing_count": 0,
        }
        if not isinstance(old, dict):
            types = ["new"]
            extract_ids.add(source_id); semantic_ids.add(source_id); affected_ids.add(source_id)
        else:
            types: list[str] = []
            if int(prior_seen.get("missing_count") or old.get("missing_count") or 0) > 0:
                types.append("restored")
                extract_ids.add(source_id); semantic_ids.add(source_id); affected_ids.add(source_id)
            if str(current.get("update_time") or "") != str(old.get("update_time") or ""):
                types.append("content_check")
                extract_ids.add(source_id); semantic_ids.add(source_id); affected_ids.add(source_id)
            elif force_hash_audit:
                # A periodic audit covers rare upstream/API cases where content
                # changes without a trustworthy update_time change. Extraction
                # and hashes are local/API work; unchanged AI inputs hit cache.
                types.append("content_check")
                extract_ids.add(source_id); semantic_ids.add(source_id); affected_ids.add(source_id)
            if str(current.get("file_name") or "") != str(old.get("file_name") or ""):
                types.append("renamed"); affected_ids.add(source_id); semantic_ids.add(source_id)
            if str(current.get("source_path") or "") != str(old.get("source_path") or ""):
                types.append("moved"); affected_ids.add(source_id); semantic_ids.add(source_id)
            metadata_pairs = (
                ("creator_uid", "creator_uid"), ("extension", "extension"),
                ("content_type", "content_type"), ("source_url", "source_url"),
            )
            # The workspace tree API may omit creator metadata that was previously
            # enriched through doc search/contact lookup. Missing current values are
            # an observation blind spot, not a metadata change and must not fan out
            # into a near-full semantic rerun.
            if any(
                str(current.get(left) or "")
                and str(current.get(left) or "") != str(old.get(right) or "")
                for left, right in metadata_pairs
            ):
                types.append("metadata_changed"); affected_ids.add(source_id); semantic_ids.add(source_id)
            permission_status = str(current.get("permission_scan_status") or "")
            current_permission = str(current.get("permission_hash") or "")
            old_permission = str(old.get("permission_hash") or "")
            if permission_status == "success" and current_permission and old_permission and current_permission != old_permission:
                types.append("permission_changed"); affected_ids.add(source_id); semantic_ids.add(source_id)
            if not types:
                types = ["unchanged"]
        changes.append({
            "source_id": source_id,
            "title": current.get("file_name", ""),
            "source_url": common.sanitize_transient_url(current.get("source_url", "")),
            "source_path": current.get("source_path", ""),
            "change_type": primary_type(types),
            "change_flags": types,
            "old_update_time": (old or {}).get("update_time", "") if isinstance(old, dict) else "",
            "current_update_time": current.get("update_time", ""),
            "old_source_hash": (old or {}).get("source_hash", "") if isinstance(old, dict) else "",
            "requires_extraction": source_id in extract_ids,
            "requires_semantic_refresh": source_id in semantic_ids,
            "requires_relation_review": source_id in affected_ids,
            "responsible_action": "自动处理" if primary_type(types) not in {"suspected_missing", "source_orphan"} else "负责人确认",
        })
        if primary_type(types) != "unchanged":
            processing_ids.add(source_id)

    for source_id, old in applied_by_id.items():
        if source_id in current_by_id or not isinstance(old, dict):
            continue
        # Embedded attachments do not appear in the workspace tree inventory.
        # Their lifecycle follows the parent document and they must not become
        # false source-orphan alerts on every incremental scan.
        if common.is_parent_managed_embedded_resource(source_id, old):
            prior = previous_observation.get(source_id) if isinstance(previous_observation.get(source_id), dict) else {}
            next_observation[source_id] = {
                "last_seen_at": prior.get("last_seen_at") or old.get("last_seen_at") or "",
                "missing_count": 0,
                "observation_scope": "parent_managed_embedded_resource",
            }
            continue
        prior = previous_observation.get(source_id) if isinstance(previous_observation.get(source_id), dict) else {}
        missing_count = int(prior.get("missing_count") or old.get("missing_count") or 0)
        if not same_observation_snapshot:
            missing_count += 1
        kind = "source_orphan" if missing_count >= max(1, missing_confirm_runs) else "suspected_missing"
        next_observation[source_id] = {
            "last_seen_at": prior.get("last_seen_at") or old.get("last_seen_at") or "",
            "missing_count": missing_count,
        }
        affected_ids.add(source_id)
        changes.append({
            "source_id": source_id,
            "title": old.get("file_name", ""),
            "source_url": common.sanitize_transient_url(old.get("source_url", "")),
            "source_path": old.get("source_path", ""),
            "change_type": kind,
            "change_flags": [kind],
            "missing_count": missing_count,
            "requires_extraction": False,
            "requires_semantic_refresh": False,
            "requires_relation_review": True,
            "responsible_action": "确认原文已删除、迁移还是权限不可见；禁止自动删除蒸馏页",
        })

    counts = Counter(row["change_type"] for row in changes)
    run = str(observed.get("snapshot_id") or common.run_id())
    plan = {
        "schema_version": common.STATE_VERSION,
        "run_id": run,
        "generated_at": common.now_iso(),
        "snapshot_id": observed.get("snapshot_id", ""),
        "baseline_established_at": applied.get("established_at", ""),
        "changes": sorted(changes, key=lambda row: (-PRIORITY.get(row["change_type"], 1), row.get("source_path", ""))),
        "counts": dict(sorted(counts.items())),
        "actions": {
            "extract_source_ids": sorted(extract_ids),
            "semantic_source_ids": sorted(semantic_ids),
            "affected_source_ids": sorted(affected_ids),
            "processing_source_ids": sorted(processing_ids),
            "requires_relation_rebuild": bool(processing_ids),
            "requires_render": bool(processing_ids),
            "requires_health_audit": True,
        },
        "status": "changes_detected" if any(key != "unchanged" and value for key, value in counts.items()) else "no_change",
    }
    common.write_json(paths["plan"], plan)
    common.write_json(paths["observation"], {
        "schema_version": common.STATE_VERSION,
        "updated_at": common.now_iso(),
        "last_snapshot_id": observed.get("snapshot_id", ""),
        "documents": next_observation,
    })
    report_rows = []
    for row in plan["changes"]:
        report_rows.append({**row, "change_flags": ",".join(row.get("change_flags") or [])})
    common.write_csv(paths["reports"] / "增量变化清单.csv", report_rows)
    common.write_json(paths["reports"] / "增量变化清单.json", plan)
    common.append_jsonl(paths["root"] / "maintenance-history.jsonl", {
        "event": "incremental_plan", "at": plan["generated_at"], "run_id": run,
        "counts": plan["counts"], "status": plan["status"],
    })
    print(f"INCREMENTAL_PLAN_OK run={run} status={plan['status']} counts={dict(counts)}")
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare the latest observation with the last successful applied state.")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--missing-confirm-runs", type=int, default=2)
    parser.add_argument("--force-hash-audit", action="store_true")
    args = parser.parse_args()
    build(args.job, missing_confirm_runs=args.missing_confirm_runs, force_hash_audit=args.force_hash_audit)


if __name__ == "__main__":
    main()
