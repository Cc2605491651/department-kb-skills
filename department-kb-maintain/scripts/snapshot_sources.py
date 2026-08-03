#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import subprocess
import sys

import maintain_common as common


def permission_snapshot(node_id: str) -> tuple[str, str, str]:
    result = subprocess.run(
        ["dws", "doc", "permission", "list", "--node", node_id, "--max-results", "50", "--format", "json"],
        capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0:
        return "", "", (result.stderr or result.stdout or "permission query failed")[-500:]
    try:
        payload = common.extract_json(result.stdout)
        if payload.get("success") is False or payload.get("error"):
            return "", "", json.dumps(payload.get("error") or {}, ensure_ascii=False)[:500]
        normalized = sorted(
            [
                {
                    "type": str(item.get("type") or ""),
                    "id": str(item.get("id") or ""),
                    "name": str(item.get("name") or ""),
                    "role": str(item.get("role") or ""),
                    "outer": bool(item.get("outer")),
                }
                for item in payload.get("permissions") or [] if isinstance(item, dict)
            ],
            key=lambda item: (item["type"], item["id"], item["role"]),
        )
        text = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return text, common.sha256_text(text), ""
    except Exception as error:  # noqa: BLE001 - preserve per-node visibility failure
        return "", "", f"{type(error).__name__}: {error}"[:500]


def scan(job: Path, base: Path, *, workers: int = 4, permission_mode: str = "off", snapshot_id: str = "") -> dict:
    job = job.resolve()
    paths = common.state_paths(job)
    snapshot_id = snapshot_id or common.run_id()
    snapshot_dir = paths["snapshots"] / snapshot_id
    if snapshot_dir.exists():
        raise RuntimeError(f"观察快照已存在：{snapshot_dir}")
    common.run_base(base, "inventory_wiki.py", ["--job", str(job), "--output-dir", str(snapshot_dir)])
    rows = common.load_json(snapshot_dir / "raw-manifest.json", [])
    if not isinstance(rows, list):
        raise RuntimeError("独立盘点没有生成有效raw-manifest.json")
    observed_at = common.now_iso()
    permission_errors: list[dict] = []
    if permission_mode == "all" and rows:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, 30))) as pool:
            futures = {
                pool.submit(permission_snapshot, str(row.get("node_id") or "")): row
                for row in rows if row.get("node_id")
            }
            for future in as_completed(futures):
                row = futures[future]
                snapshot, permission_hash, error = future.result()
                if error:
                    row["permission_scan_status"] = "error"
                    row["permission_scan_error"] = error
                    permission_errors.append({"source_id": row.get("source_id", ""), "error": error})
                else:
                    row["permission_snapshot"] = snapshot
                    row["permission_hash"] = permission_hash
                    row["permission_scan_status"] = "success"
    for row in rows:
        row["observed_at"] = observed_at
        row["path_hash"] = common.fingerprint(row.get("source_path", ""))
        row["metadata_hash"] = common.fingerprint(
            row.get("file_name", ""), row.get("source_path", ""), row.get("extension", ""),
            row.get("content_type", ""), row.get("creator_uid", ""), row.get("update_time", ""),
        )
        if permission_mode != "all":
            row["permission_scan_status"] = "not_scanned"
    payload = {
        "schema_version": common.STATE_VERSION,
        "snapshot_id": snapshot_id,
        "observed_at": observed_at,
        "workspace_id": common.task_value(job, "source.workspace_id", ""),
        "permission_scan": permission_mode,
        "permission_error_count": len(permission_errors),
        "documents": sorted(rows, key=lambda row: str(row.get("source_path") or "")),
        "tree": common.load_json(snapshot_dir / "raw-tree.json", []),
    }
    common.write_json(snapshot_dir / "observed-snapshot.json", payload)
    common.write_json(paths["latest"], payload)
    common.write_json(snapshot_dir / "raw-manifest.json", rows)
    common.append_jsonl(paths["root"] / "maintenance-history.jsonl", {
        "event": "source_scan", "at": observed_at, "snapshot_id": snapshot_id,
        "documents": len(rows), "permission_scan": permission_mode,
        "permission_errors": len(permission_errors),
    })
    common.write_json(paths["reports"] / "增量扫描结果.json", {
        "snapshot_id": snapshot_id, "observed_at": observed_at,
        "documents": len(rows), "permission_errors": permission_errors,
    })
    print(f"INCREMENTAL_SCAN_OK snapshot={snapshot_id} documents={len(rows)} permission_errors={len(permission_errors)}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an independent read-only source observation snapshot.")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--base-skill-root", default="")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--permission-scan", choices=["off", "all"], default="off")
    parser.add_argument("--snapshot-id", default="")
    args = parser.parse_args()
    scan(args.job, common.find_base_skill(args.base_skill_root), workers=args.workers, permission_mode=args.permission_scan, snapshot_id=args.snapshot_id)


if __name__ == "__main__":
    main()
