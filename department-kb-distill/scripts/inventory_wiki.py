#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any

from config_utils import load_config


HEADERS = [
    "source_id", "department", "source_path", "file_name", "node_id", "source_url",
    "node_type", "content_type", "extension", "create_time", "update_time", "creator_uid", "creator_name", "owner", "permission_snapshot", "snapshot_status",
    "virtual_kind", "parent_source_id", "parent_node_id", "resource_id",
    "parse_status", "first_attempt", "second_attempt", "last_error",
    "source_hash", "extracted_hash", "extracted_chars", "raw_mirror_path", "business_path",
    "raw_page_url", "business_page_url", "processing", "status", "delivery_status", "delivery_error",
    "failure_stage", "failure_category", "attempt_count", "last_attempt_at", "download_bytes", "file_signature", "http_status",
]


def run_dws(args: list[str]) -> dict:
    """执行只读 DWS 命令。

    大型知识库需要连续请求很多文件夹，偶发的网络或服务端失败不应让整次
    盘点立即中止。首次失败时按 DWS 规则带 --verbose 重试一次；如仍失败，
    保留具体命令和简短错误，便于定位到对应文件夹。
    """
    command = ["dws", *args, "--format", "json"]
    errors: list[str] = []
    for attempt in range(2):
        attempt_command = command if attempt == 0 or "--verbose" in command else [*command, "--verbose"]
        try:
            result = subprocess.run(attempt_command, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            errors.append("命令超时180秒")
            continue

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or f"退出码 {result.returncode}").strip()
            errors.append(detail[-800:])
            continue

        start, end = result.stdout.find("{"), result.stdout.rfind("}")
        if start < 0 or end < start:
            errors.append("DWS 未返回 JSON")
            continue
        try:
            payload = json.loads(result.stdout[start:end + 1])
        except json.JSONDecodeError as exc:
            errors.append(f"DWS JSON 解析失败：{exc}")
            continue
        if payload.get("success") is False or payload.get("error"):
            detail = json.dumps(payload.get("error") or payload, ensure_ascii=False)[:800]
            errors.append(f"DWS 返回业务错误：{detail}")
            continue
        return payload

    detail = errors[-1] if errors else "未知错误"
    raise RuntimeError(f"DWS 只读命令失败（已重试 1 次）：{' '.join(args)}；{detail}")


def stable_id(workspace: str, node_id: str, prefix: str) -> str:
    digest = hashlib.sha256(f"{workspace}:{node_id}".encode()).hexdigest()[:12].upper()
    return f"{prefix}-{digest}"


def nodes(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        if isinstance(payload.get("nodes"), list):
            return [item for item in payload["nodes"] if isinstance(item, dict)]
        for value in payload.values():
            found = nodes(value)
            if found:
                return found
    return []


def list_folder(workspace: str, folder: str = "") -> list[dict]:
    output: list[dict] = []
    token = ""
    while True:
        args = ["doc", "list", "--page-size", "50"]
        if folder:
            args.extend(["--folder", folder])
        else:
            args.extend(["--workspace", workspace])
        if token:
            args.extend(["--page-token", token])
        payload = run_dws(args)
        output.extend(nodes(payload))
        if not payload.get("hasMore"):
            break
        token = str(payload.get("nextPageToken") or payload.get("pageToken") or "")
        if not token:
            raise RuntimeError("DWS 标记 hasMore 但未返回 page token")
    return output


def extension(node: dict) -> str:
    value = str(node.get("extension") or "").lower().lstrip(".")
    if value:
        return value
    content_type = str(node.get("contentType") or "").upper()
    if content_type == "ALIDOC":
        return "adoc"
    suffix = Path(str(node.get("name") or "")).suffix.lower().lstrip(".")
    return suffix or "bin"


def creator_fields(node: dict) -> tuple[str, str]:
    creator = node.get("creator") if isinstance(node.get("creator"), dict) else {}
    uid = str(
        node.get("creatorUid") or node.get("creatorId") or node.get("creatorUserId")
        or creator.get("userId") or creator.get("uid") or creator.get("id") or ""
    )
    name = str(node.get("creatorName") or creator.get("name") or creator.get("displayName") or "")
    return uid, name


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", delete=False, dir=path.parent) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)
        temp = Path(handle.name)
    temp.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only recursive DingTalk knowledge-base inventory.")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--department", default="")
    parser.add_argument("--id-prefix", default="")
    parser.add_argument("--include-prefix", action="append", default=[])
    parser.add_argument("--exclude-prefix", action="append", default=[])
    parser.add_argument("--output-dir", type=Path, default=None, help="将只读盘点写入独立目录；默认写入任务的01-inventory")
    args = parser.parse_args()
    job = args.job.resolve()
    config = load_config(job)
    workspace_input = args.workspace or str(config.get("source.workspace_id", "workspace_id", "source.workspace_url", "workspace_url", default="") or "")
    if not workspace_input:
        raise SystemExit("必须通过参数或 task-config.yaml 提供 source_workspace_id")
    workspace_match = re.search(r"/spaces/([^/]+)", workspace_input)
    workspace = workspace_match.group(1) if workspace_match else workspace_input
    department = args.department or str(config.get("source.department", "department", default="部门") or "部门")
    id_prefix = (args.id_prefix or str(config.get("source.stable_id_prefix", "stable_id_prefix", "id_prefix", default="KB") or "KB")).upper()
    configured_includes = [str(value) for value in config.get_list("source.include_paths", "include_paths")]
    configured_excludes = [str(value) for value in config.get_list("source.exclude_paths", "excluded_source_paths", "exclude_paths")]
    raw_includes = [*args.include_prefix, *configured_includes]
    include_prefixes = [value.strip("/") for value in raw_includes if value.strip() not in {"", "/"}]
    exclude_prefixes = [value.strip("/") for value in [*args.exclude_prefix, *configured_excludes] if value.strip() not in {"", "/"}]

    def is_excluded(path: str) -> bool:
        return any(path == prefix or path.startswith(prefix + "/") for prefix in exclude_prefixes)

    def is_included(path: str) -> bool:
        return not include_prefixes or any(path == prefix or path.startswith(prefix + "/") for prefix in include_prefixes)

    def may_contain_included(path: str) -> bool:
        return not include_prefixes or is_included(path) or any(prefix.startswith(path + "/") for prefix in include_prefixes)
    inventory = args.output_dir.resolve() if args.output_dir else job / "01-inventory"
    reports = job / "06-reports"
    pending: list[tuple[str, str, str, str]] = [("", "", "", "")]
    seen_folders: set[str] = set()
    files: list[dict] = []
    folder_rows: list[dict] = []
    tree: list[dict] = []
    while pending:
        folder_id, parent_path, parent_source_id, parent_node_id = pending.pop(0)
        if folder_id in seen_folders:
            continue
        seen_folders.add(folder_id)
        children = list_folder(workspace, folder_id)
        for node in children:
            name = str(node.get("name") or "未命名")
            path = f"{parent_path}/{name}".strip("/")
            if is_excluded(path):
                continue
            node_id = str(node.get("nodeId") or node.get("id") or "")
            if not node_id:
                continue
            sid = stable_id(workspace, node_id, id_prefix)
            item = {"name": name, "node_id": node_id, "path": path, "parent_node_id": folder_id, "node_type": node.get("nodeType", ""), "source_id": sid}
            tree.append(item)
            if str(node.get("nodeType") or "").lower() == "folder" or node.get("hasChildren") and not node.get("contentType"):
                if not may_contain_included(path):
                    continue
                folder_rows.append(item)
                pending.append((node_id, path, sid, folder_id))
                continue
            if not is_included(path):
                continue
            row = {field: "" for field in HEADERS}
            creator_uid, creator_name = creator_fields(node)
            row.update({
                "source_id": sid, "department": department, "source_path": path, "file_name": name,
                "node_id": node_id, "source_url": node.get("docUrl") or node.get("url") or "",
                "node_type": node.get("nodeType") or "file", "content_type": node.get("contentType") or "",
                "extension": extension(node), "create_time": node.get("createTime") or "", "update_time": node.get("updateTime") or "",
                "creator_uid": creator_uid, "creator_name": creator_name, "owner": creator_name or "待补充",
                "snapshot_status": "任务启动时已发现", "parent_source_id": parent_source_id,
                "parent_node_id": folder_id or parent_node_id, "parse_status": "待解析", "processing": "待处理", "status": "候选",
            })
            files.append(row)
        print(f"INVENTORY_PROGRESS folders={len(seen_folders)} files={len(files)} pending={len(pending)}", flush=True)
    files.sort(key=lambda row: row["source_path"])
    write_csv(inventory / "raw-manifest.csv", files, HEADERS)
    atomic_json(inventory / "raw-manifest.json", files)
    write_csv(inventory / "directory-map.csv", folder_rows, ["source_id", "node_id", "parent_node_id", "path", "name", "node_type"])
    atomic_json(inventory / "raw-tree.json", tree)
    snapshot = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    report = f"# 知识库只读盘点\n\n- workspace_id: {workspace}\n- source_snapshot_at: {snapshot}\n- folders: {len(folder_rows)}\n- files: {len(files)}\n- included_prefixes: {', '.join(include_prefixes) or '全部'}\n- excluded_prefixes: {', '.join(exclude_prefixes) or '无'}\n- inventory_mode: read_only\n"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "inventory-progress.md").write_text(report, encoding="utf-8")
    print(f"INVENTORY_OK workspace={workspace} folders={len(folder_rows)} files={len(files)}")


if __name__ == "__main__":
    main()
