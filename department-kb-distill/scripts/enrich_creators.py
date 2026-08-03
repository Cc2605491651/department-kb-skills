#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import csv
from pathlib import Path
import re
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_common as common
from inventory_wiki import HEADERS, run_dws


def search_documents(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    for key in ("documents", "nodes"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    for value in payload.values():
        found = search_documents(value)
        if found:
            return found
    return []


def creator_from_search(payload: dict, node_id: str) -> tuple[str, str]:
    for document in search_documents(payload):
        candidate_id = str(document.get("nodeId") or document.get("id") or "")
        if candidate_id != node_id:
            continue
        creator = document.get("creator") if isinstance(document.get("creator"), dict) else {}
        uid = str(
            document.get("creatorUid") or document.get("creatorId") or document.get("creatorUserId")
            or creator.get("userId") or creator.get("uid") or creator.get("id") or ""
        )
        name = str(document.get("creatorName") or creator.get("name") or creator.get("displayName") or "")
        return uid, name
    return "", ""


def contact_name_map(payload: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    values = payload.get("result") if isinstance(payload, dict) else []
    if not isinstance(values, list):
        return result
    for item in values:
        if not isinstance(item, dict):
            continue
        model = item.get("orgEmployeeModel") if isinstance(item.get("orgEmployeeModel"), dict) else item
        uid = str(model.get("orgUserId") or model.get("userId") or item.get("userId") or "")
        name = str(model.get("orgUserName") or model.get("name") or item.get("name") or "")
        if uid and name:
            result[uid] = name
    return result


def search_creator(workspace: str, row: dict, max_pages: int = 10) -> tuple[str, str]:
    node_id = str(row.get("node_id") or "")
    title = str(row.get("file_name") or "").strip()
    if not node_id or not title:
        return "", ""
    for query in creator_search_queries(title):
        cursor = ""
        for _ in range(max_pages):
            args = [
                "doc", "search", "--query", query,
                "--workspace-ids", workspace, "--limit", "30",
            ]
            if cursor:
                args.extend(["--cursor", cursor])
            payload = run_dws(args)
            uid, name = creator_from_search(payload, node_id)
            if uid or name:
                return uid, name
            if not payload.get("hasMore"):
                break
            cursor = str(payload.get("nextPageToken") or "")
            if not cursor:
                break
    return "", ""


def chunks(values: list[str], size: int = 30) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def creator_search_queries(title: str) -> list[str]:
    normalized = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff]+", " ", title).strip()
    shortened = normalized[:32].strip()
    return list(dict.fromkeys(value for value in (title.strip(), normalized, shortened) if value))


def write_manifest(job: Path, rows: list[dict]) -> None:
    inventory = job / "01-inventory"
    common.write_json(inventory / "raw-manifest.json", rows)
    csv_path = inventory / "raw-manifest.csv"
    fields = list(HEADERS)
    if csv_path.exists():
        with csv_path.open(encoding="utf-8", newline="") as handle:
            fields = list(csv.DictReader(handle).fieldnames or HEADERS)
    common.write_csv(csv_path, rows, fields)


def update_existing_source_profiles(job: Path, rows: list[dict]) -> int:
    paths = common.job_paths(job)
    changed = 0
    for row in rows:
        path = paths["source_profiles"] / f"{row.get('source_id', '')}.json"
        profile = common.load_json(path)
        if not isinstance(profile, dict):
            continue
        uid = str(row.get("creator_uid") or "")
        name = str(row.get("creator_name") or "")
        if (
            str(profile.get("creator_uid") or "") == uid
            and str(profile.get("creator_name") or "") == name
            and str(profile.get("owner") or "") == (name or "待补充")
        ):
            continue
        profile["creator_uid"] = uid
        profile["creator_name"] = name
        profile["owner"] = name or "待补充"
        common.write_json(path, profile)
        changed += 1
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve DingTalk document creators and write creator names to Owner.")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--workspace", default="")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    job = args.job.resolve()
    config = common.load_task_config(job)
    workspace = args.workspace.strip() or str(config.get("source.workspace_id", "workspace_id", default="") or "")
    if not workspace:
        raise SystemExit("必须提供 source.workspace_id 才能限定创建者补查范围")
    rows = common.load_manifest(job)
    errors: list[dict] = []
    pending_indexes = [
        index for index, row in enumerate(rows)
        if not str(row.get("creator_uid") or "")
    ]
    workers = max(1, min(args.workers, 30))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(search_creator, workspace, rows[index]): index
            for index in pending_indexes
        }
        completed = 0
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            try:
                uid, name = future.result()
                if uid:
                    rows[index]["creator_uid"] = uid
                if name:
                    rows[index]["creator_name"] = name
            except Exception as error:
                errors.append({
                    "source_id": rows[index].get("source_id", ""),
                    "stage": "doc_search",
                    "error": common.safe_error(error),
                })
            completed += 1
            if completed % 50 == 0 or completed == len(pending_indexes):
                print(f"CREATOR_SEARCH_PROGRESS completed={completed} total={len(pending_indexes)}", flush=True)

    uids = sorted({
        str(row.get("creator_uid") or "")
        for row in rows
        if row.get("creator_uid") and not row.get("creator_name")
    })
    names: dict[str, str] = {}
    for batch in chunks(uids):
        try:
            payload = run_dws(["contact", "user", "get", "--ids", ",".join(batch)])
            names.update(contact_name_map(payload))
        except Exception as error:
            errors.append({
                "source_id": "",
                "stage": "contact_user_get",
                "uids": batch,
                "error": common.safe_error(error),
            })
    for row in rows:
        uid = str(row.get("creator_uid") or "")
        if not row.get("creator_name") and uid in names:
            row["creator_name"] = names[uid]
        row["owner"] = str(row.get("creator_name") or "待补充")

    write_manifest(job, rows)
    profile_updates = update_existing_source_profiles(job, rows)
    resolved = sum(bool(row.get("creator_name")) for row in rows)
    unresolved = [
        {
            "source_id": row.get("source_id", ""),
            "node_id": row.get("node_id", ""),
            "title": row.get("file_name", ""),
            "creator_uid": row.get("creator_uid", ""),
        }
        for row in rows if not row.get("creator_name")
    ]
    report = {
        "workspace_id": workspace,
        "total": len(rows),
        "resolved": resolved,
        "unresolved": len(unresolved),
        "profile_updates": profile_updates,
        "unresolved_documents": unresolved,
        "errors": errors,
        "generated_at": common.now_iso(),
    }
    common.write_json(common.job_paths(job)["reports"] / "creator-enrichment.json", report)
    label = "OK" if not unresolved else "PARTIAL"
    print(f"CREATOR_ENRICHMENT_{label} total={len(rows)} resolved={resolved} unresolved={len(unresolved)} errors={len(errors)} profile_updates={profile_updates}")


if __name__ == "__main__":
    main()
