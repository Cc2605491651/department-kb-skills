#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_common as common
from config_utils import is_local_only


FIELD_SPECS = [
    ("relation_id", "关系ID", "text"), ("source_id", "来源稳定ID", "text"),
    ("source_title", "来源文档", "text"), ("source_url", "来源链接", "url"),
    ("source_creator_uid", "来源创建者ID", "text"), ("source_creator_name", "来源创建者", "text"),
    ("target_id", "目标稳定ID", "text"), ("target_title", "目标文档", "text"),
    ("target_url", "目标链接", "url"), ("target_creator_uid", "目标创建者ID", "text"),
    ("target_creator_name", "目标创建者", "text"), ("business_relation_name", "关系", "text"),
    ("proposed_type", "后台系统关系标签", "text"),
    ("relation_type_explanation", "标签通俗解释", "text"), ("direction", "方向", "text"),
    ("relation_meaning", "本条关系具体含义", "text"), ("l3_reason", "为什么列为L3", "text"),
    ("confirmation_question", "负责人需要确认", "text"), ("risk_flags", "风险标记", "text"),
    ("rule_ids", "召回规则", "text"), ("local_evidence", "本地证据", "text"),
    ("codex_evidence", "Codex证据", "text"), ("risk_level", "风险等级", "singleSelect"),
    ("review_level", "确认级别", "singleSelect"), ("reviewer", "确认人", "text"),
    ("review_status", "确认状态", "singleSelect"), ("review_comment", "确认意见", "text"),
    ("modified_type", "修改后关系", "text"), ("confirmed_at", "确认时间", "date"),
    ("published_at", "发布时间", "date"), ("readback_status", "回读状态", "singleSelect"),
    ("archive_status", "归档状态", "singleSelect"), ("re_review_at", "复审时间", "date"),
    ("review_input_hash", "证据版本", "text"), ("queue_updated_at", "清单更新时间", "date"),
]
DECISION_KEYS = {
    "reviewer", "review_status", "review_comment", "modified_type", "confirmed_at", "published_at",
    "readback_status", "archive_status", "re_review_at",
}


def parse_json_output(value: str) -> dict:
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError("DWS 未返回 JSON")
    payload = json.loads(value[start:end + 1])
    if payload.get("success") is False:
        raise RuntimeError("DWS 返回 success=false")
    return payload


def run_dws(args: list[str], timeout: int = 180) -> dict:
    result = subprocess.run(["dws", *args, "--format", "json"], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"DWS 命令失败：{' '.join(args[:4])} exit={result.returncode}")
    return parse_json_output(result.stdout)


def find_key(value: Any, keys: set[str]) -> str:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in keys and isinstance(nested, str) and nested:
                return nested
        for nested in value.values():
            found = find_key(nested, keys)
            if found:
                return found
    if isinstance(value, list):
        for nested in value:
            found = find_key(nested, keys)
            if found:
                return found
    return ""


def find_fields(value: Any) -> list[dict]:
    result: list[dict] = []
    if isinstance(value, dict):
        field_id = value.get("fieldId") or value.get("id") if (value.get("fieldName") or value.get("name")) else None
        name = value.get("fieldName") or value.get("name")
        if field_id and name and str(field_id).startswith(("fld", "f")):
            result.append({"fieldId": str(field_id), "name": str(name)})
        for nested in value.values():
            result.extend(find_fields(nested))
    elif isinstance(value, list):
        for nested in value:
            result.extend(find_fields(nested))
    unique = {item["fieldId"]: item for item in result}
    return list(unique.values())


def find_records(value: Any) -> list[dict]:
    result: list[dict] = []
    if isinstance(value, dict):
        if (value.get("recordId") or value.get("id")) and isinstance(value.get("cells"), dict):
            result.append({"recordId": value.get("recordId") or value.get("id"), "cells": value["cells"]})
        for nested in value.values():
            result.extend(find_records(nested))
    elif isinstance(value, list):
        for nested in value:
            result.extend(find_records(nested))
    unique = {str(item["recordId"]): item for item in result}
    return list(unique.values())


def load_queue(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def batch(values: list[Any], size: int = 30) -> Iterable[list[Any]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def field_config(name: str, kind: str) -> dict:
    config: dict[str, Any] = {"fieldName": name, "type": kind}
    if name == "确认状态":
        config["config"] = {"options": [{"name": value} for value in ["待确认", "已确认", "已驳回", "修改后确认", "暂缓", "已发布", "已归档", "待复审"]]}
    elif name == "确认级别":
        config["config"] = {"options": [{"name": "L3"}]}
    elif name == "风险等级":
        config["config"] = {"options": [{"name": "高"}]}
    elif name == "回读状态":
        config["config"] = {"options": [{"name": value} for value in ["待发布", "待回读", "回读通过", "回读失败"]]}
    elif name == "归档状态":
        config["config"] = {"options": [{"name": value} for value in ["未归档", "已归档"]]}
    return config


def ensure_remote(paths: dict[str, Path], department: str) -> dict:
    state_path = paths["ledgers"] / "review-aitable-state.json"
    state = common.load_json(state_path, {}) or {}
    if not state.get("base_id"):
        payload = run_dws(["aitable", "base", "create", "--name", f"{department}-知识库L3高风险关系确认"])
        state["base_id"] = find_key(payload, {"baseId", "base_id"})
        if not state["base_id"]:
            raise RuntimeError("创建 AI 表格后未取得 baseId")
        state["created_at"] = common.now_iso()
        common.write_json(state_path, state)
    if not state.get("table_id"):
        first_fields = [field_config(name, kind) for _, name, kind in FIELD_SPECS[:15]]
        payload = run_dws([
            "aitable", "table", "create", "--base-id", state["base_id"],
            "--name", "L3高风险关系确认清单", "--fields", json.dumps(first_fields, ensure_ascii=False),
        ])
        state["table_id"] = find_key(payload, {"tableId", "table_id", "sheetId"})
        if not state["table_id"]:
            raise RuntimeError("创建数据表后未取得 tableId")
        common.write_json(state_path, state)
        remaining = [field_config(name, kind) for _, name, kind in FIELD_SPECS[15:]]
        for fields in batch(remaining, 15):
            run_dws([
                "aitable", "field", "create", "--base-id", state["base_id"], "--table-id", state["table_id"],
                "--fields", json.dumps(fields, ensure_ascii=False),
            ])
    table = run_dws(["aitable", "table", "get", "--base-id", state["base_id"], "--table-ids", state["table_id"]])
    fields = find_fields(table)
    by_name = {field["name"]: field["fieldId"] for field in fields}
    missing = [name for _, name, _ in FIELD_SPECS if name not in by_name]
    if missing:
        additions = [field_config(name, kind) for _, name, kind in FIELD_SPECS if name in missing]
        for fields_batch in batch(additions, 15):
            run_dws([
                "aitable", "field", "create", "--base-id", state["base_id"], "--table-id", state["table_id"],
                "--fields", json.dumps(fields_batch, ensure_ascii=False),
            ])
        table = run_dws(["aitable", "table", "get", "--base-id", state["base_id"], "--table-ids", state["table_id"]])
        by_name = {field["name"]: field["fieldId"] for field in find_fields(table)}
    state["field_ids"] = {key: by_name[name] for key, name, _ in FIELD_SPECS if name in by_name}
    state["last_schema_readback_at"] = common.now_iso()
    common.write_json(state_path, state)
    return state


def scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("name") or value.get("text") or value.get("value") or "")
    if isinstance(value, list):
        return ",".join(scalar(item) for item in value)
    return str(value)


def cells_for_row(row: dict, field_ids: dict[str, str], *, include_decisions: bool) -> dict:
    cells: dict[str, Any] = {}
    for key, _, kind in FIELD_SPECS:
        if key not in field_ids or (not include_decisions and key in DECISION_KEYS):
            continue
        value = row.get(key, "")
        if value == "" and kind == "date":
            continue
        if kind == "url" and value:
            value = {"text": row.get("source_title" if key == "source_url" else "target_title", value), "link": value}
        cells[field_ids[key]] = value
    return cells


def main() -> None:
    parser = argparse.ArgumentParser(description="Create and sync the DingTalk AI-table review queue.")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--create-only", action="store_true")
    args = parser.parse_args()
    paths = common.job_paths(args.job)
    config = common.load_task_config(args.job)
    if is_local_only(config):
        raise SystemExit("LOCAL_ONLY_GUARD: 当前任务禁止任何钉钉线上写入或 AI 表格同步")
    if not config.bool("review_sync.enabled", default=False):
        raise SystemExit("AITABLE_SYNC_DISABLED: 发布目录链接不授权AI表格写入；需在task-config.yaml单独启用review_sync.enabled")
    department = str(config.get("source.department", "department", default="部门") or "部门")
    state = ensure_remote(paths, department)
    queue_path = paths["ledgers"] / "relation-review-queue.csv"
    rows = load_queue(queue_path)
    if args.create_only or not rows:
        print(f"REVIEW_AITABLE_READY base_id={state['base_id']} table_id={state['table_id']} rows=0")
        return
    query = run_dws([
        "aitable", "record", "query", "--base-id", state["base_id"], "--table-id", state["table_id"], "--all", "--page-limit", "0",
    ], timeout=600)
    records = find_records(query)
    relation_field = state["field_ids"]["relation_id"]
    existing = {scalar(record["cells"].get(relation_field)): record for record in records if scalar(record["cells"].get(relation_field))}

    # Pull business decisions before pushing regenerated evidence.
    decision_by_field = {state["field_ids"][key]: key for key in DECISION_KEYS if key in state["field_ids"]}
    by_relation = {row["relation_id"]: row for row in rows}
    for relation_id, record in existing.items():
        row = by_relation.get(relation_id)
        if not row:
            continue
        for field_id, key in decision_by_field.items():
            value = scalar(record["cells"].get(field_id))
            if value:
                row[key] = value
    common.write_csv(queue_path, rows, list(rows[0].keys()))

    creates: list[dict] = []
    updates: list[dict] = []
    for row in rows:
        record = existing.get(row["relation_id"])
        if record:
            updates.append({"recordId": record["recordId"], "cells": cells_for_row(row, state["field_ids"], include_decisions=False)})
        else:
            creates.append({"cells": cells_for_row(row, state["field_ids"], include_decisions=True)})
    for operation, values in (("create", creates), ("update", updates)):
        for records_batch in batch(values, 30):
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
                json.dump(records_batch, handle, ensure_ascii=False)
                records_file = Path(handle.name)
            try:
                run_dws([
                    "aitable", "record", operation, "--base-id", state["base_id"], "--table-id", state["table_id"],
                    "--records-file", str(records_file),
                ], timeout=600)
            finally:
                records_file.unlink(missing_ok=True)
    readback = run_dws([
        "aitable", "record", "query", "--base-id", state["base_id"], "--table-id", state["table_id"], "--all", "--page-limit", "0",
    ], timeout=600)
    readback_records = find_records(readback)
    remote_ids = {scalar(record["cells"].get(relation_field)) for record in readback_records}
    missing = [row["relation_id"] for row in rows if row["relation_id"] not in remote_ids]
    if missing:
        raise RuntimeError(f"AI 表格回读缺少 {len(missing)} 条关系")
    state["last_sync_at"] = common.now_iso()
    state["last_sync_rows"] = len(rows)
    common.write_json(paths["ledgers"] / "review-aitable-state.json", state)
    print(f"REVIEW_AITABLE_SYNC_OK created={len(creates)} updated={len(updates)} readback={len(readback_records)}")


if __name__ == "__main__":
    main()
