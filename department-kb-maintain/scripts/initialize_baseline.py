#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import maintain_common as common


def initialize(job: Path, *, force: bool = False) -> dict:
    job = job.resolve()
    paths = common.state_paths(job)
    if paths["applied"].exists() and not force:
        value = common.load_json(paths["applied"], {})
        print(f"BASELINE_EXISTS documents={len((value or {}).get('documents') or {})}")
        return value
    acceptance = common.load_json(job / "06-reports" / "local-acceptance.json", {})
    if not isinstance(acceptance, dict) or acceptance.get("passed") is not True:
        raise RuntimeError("建立增量基线前，现有全量蒸馏必须通过local-acceptance.json本地验收")
    rows = common.load_json(job / "01-inventory" / "raw-manifest.json", [])
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("缺少现有全量盘点清单；首次建设请先使用department-kb-distill")
    established = common.now_iso()
    documents = {
        str(row.get("source_id")): common.document_state(row, observed_at=established)
        for row in rows if isinstance(row, dict) and row.get("source_id")
    }
    state = {
        "schema_version": common.STATE_VERSION,
        "established_at": established,
        "reason": "existing_full_distillation",
        "task_id": common.task_value(job, "task_id", ""),
        "workspace_id": common.task_value(job, "source.workspace_id", ""),
        "documents": documents,
        "last_successful_run": {
            "kind": "baseline",
            "local_acceptance": True,
            "publication_state_present": (job / "05-ledgers" / "钉钉发布状态.json").exists(),
        },
    }
    observation = {
        "schema_version": common.STATE_VERSION,
        "updated_at": established,
        "documents": {
            source_id: {"last_seen_at": established, "missing_count": 0}
            for source_id in documents
        },
    }
    common.write_json(paths["applied"], state)
    common.write_json(paths["observation"], observation)
    common.append_jsonl(paths["root"] / "maintenance-history.jsonl", {
        "event": "baseline_initialized", "at": established, "documents": len(documents),
    })
    print(f"BASELINE_OK documents={len(documents)}")
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize incremental maintenance from an accepted full distillation.")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    initialize(args.job, force=args.force)


if __name__ == "__main__":
    main()
