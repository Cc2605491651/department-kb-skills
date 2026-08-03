#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import re

import maintain_common as common


LOCAL_RECORD = "incremental-local-acceptance.json"
CONFIRMATION_RECORD = "maintenance-confirmation.json"
EXPLICIT_CONFIRMATION = re.compile(
    r"(?:确认|审核|验收).{0,12}(?:本次|此次|本轮)?(?:增量)?(?:蒸馏|维护|结果)?.{0,8}(?:没有问题|无问题|通过|可以发布|可以提交)"
    r"|(?:本次|此次|本轮)(?:增量)?(?:蒸馏|维护|结果).{0,12}(?:确认|审核|验收)(?:通过|无问题)"
)


def record_path(job: Path) -> Path:
    return job.resolve() / "06-reports" / LOCAL_RECORD


def confirmation_path(job: Path) -> Path:
    return common.state_paths(job)["root"] / CONFIRMATION_RECORD


def plan_hash(job: Path) -> str:
    plan = common.load_json(common.state_paths(job)["plan"], {}) or {}
    if not isinstance(plan, dict) or not plan.get("run_id"):
        raise RuntimeError("缺少本轮增量计划，不能建立审核记录")
    return common.canonical_hash(plan)


def working_set_hash(job: Path) -> str:
    rows = common.load_json(job.resolve() / "01-inventory" / "raw-manifest.json", []) or []
    values = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("source_id"):
            continue
        values.append({
            "source_id": str(row.get("source_id") or ""),
            "file_name": str(row.get("file_name") or ""),
            "source_path": str(row.get("source_path") or ""),
            "update_time": str(row.get("update_time") or ""),
            "source_hash": str(row.get("source_hash") or ""),
            "extracted_hash": str(row.get("extracted_hash") or ""),
            "parse_status": str(row.get("parse_status") or ""),
            "creator_uid": str(row.get("creator_uid") or ""),
            "creator_name": str(row.get("creator_name") or ""),
        })
    return common.canonical_hash(sorted(values, key=lambda row: row["source_id"]))


def current_identity(job: Path) -> dict:
    paths = common.state_paths(job)
    plan = common.load_json(paths["plan"], {}) or {}
    latest = common.load_json(paths["latest"], {}) or {}
    run = str(plan.get("run_id") or "")
    snapshot = str(plan.get("snapshot_id") or "")
    if not run or not snapshot:
        raise RuntimeError("本轮增量计划缺少run_id或snapshot_id")
    if str(latest.get("snapshot_id") or "") != snapshot:
        raise RuntimeError("扫描快照已变化；请重新生成计划和本地结果后再审核")
    if str(plan.get("phase") or "") != "post_extraction":
        raise RuntimeError("增量计划尚未完成正文哈希复核，不能审核")
    return {
        "run_id": run,
        "snapshot_id": snapshot,
        "plan_hash": plan_hash(job),
        "working_set_hash": working_set_hash(job),
    }


def record_local_acceptance(job: Path, processed_source_ids: list[str]) -> dict:
    job = job.resolve()
    base_acceptance = common.load_json(job / "06-reports" / "local-acceptance.json", {}) or {}
    if not isinstance(base_acceptance, dict) or base_acceptance.get("passed") is not True:
        raise RuntimeError("全量蒸馏引擎的本地验收未通过，不能生成本轮审核记录")
    identity = current_identity(job)
    record = {
        "schema_version": "incremental-local-acceptance-v1",
        **identity,
        "passed": True,
        "accepted_at": common.now_iso(),
        "processed_source_ids": sorted({str(value) for value in processed_source_ids if value}),
        "base_acceptance_hash": common.canonical_hash(base_acceptance),
    }
    common.write_json(record_path(job), record)
    # A newly generated local result invalidates any older human confirmation.
    confirmation_path(job).unlink(missing_ok=True)
    common.append_jsonl(common.state_paths(job)["root"] / "maintenance-history.jsonl", {
        "event": "incremental_local_acceptance", "at": record["accepted_at"],
        "run_id": record["run_id"], "processed_sources": len(record["processed_source_ids"]),
    })
    return record


def validate_local_acceptance(job: Path) -> dict:
    job = job.resolve()
    record = common.load_json(record_path(job), {}) or {}
    if not isinstance(record, dict) or record.get("passed") is not True:
        raise RuntimeError("缺少本轮增量本地验收记录；请先执行--stage all或--stage apply")
    identity = current_identity(job)
    for key, value in identity.items():
        if str(record.get(key) or "") != str(value or ""):
            raise RuntimeError(f"本地验收记录已过期（{key}不一致）；请重新生成本地结果")
    base_acceptance = common.load_json(job / "06-reports" / "local-acceptance.json", {}) or {}
    if base_acceptance.get("passed") is not True:
        raise RuntimeError("当前本地验收已不通过")
    if str(record.get("base_acceptance_hash") or "") != common.canonical_hash(base_acceptance):
        raise RuntimeError("全量引擎验收结果已变化；请重新执行本地增量处理")
    return record


def record_confirmation(job: Path, confirmation_text: str, confirmed_by: str) -> dict:
    text = confirmation_text.strip()
    reviewer = confirmed_by.strip()
    if not reviewer or reviewer.startswith("<"):
        raise RuntimeError("必须提供本次增量审核人的真实姓名：--confirmed-by")
    if not EXPLICIT_CONFIRMATION.search(text):
        raise RuntimeError("确认原话必须明确表达“本次增量蒸馏/维护审核通过或没有问题”")
    local = validate_local_acceptance(job)
    record = {
        "schema_version": "maintenance-confirmation-v1",
        "decision": "confirmed",
        "resulting_status": "正式",
        "confirmed_at": common.now_iso(),
        "confirmed_by": reviewer,
        "confirmation_channel": "AI对话框",
        "confirmation_text": text,
        "run_id": local["run_id"],
        "snapshot_id": local["snapshot_id"],
        "plan_hash": local["plan_hash"],
        "working_set_hash": local["working_set_hash"],
        "local_acceptance_hash": common.canonical_hash(local),
    }
    common.write_json(confirmation_path(job), record)
    common.append_jsonl(common.state_paths(job)["root"] / "maintenance-history.jsonl", {
        "event": "maintenance_confirmed", "at": record["confirmed_at"],
        "run_id": record["run_id"], "confirmed_by": reviewer,
    })
    return record


def validate_confirmation(job: Path) -> dict:
    local = validate_local_acceptance(job)
    record = common.load_json(confirmation_path(job), {}) or {}
    if not isinstance(record, dict) or record.get("decision") != "confirmed":
        raise RuntimeError("本轮尚未由执行审核人在AI对话框明确确认；请先执行--stage confirm")
    expected = {
        "run_id": local["run_id"],
        "snapshot_id": local["snapshot_id"],
        "plan_hash": local["plan_hash"],
        "working_set_hash": local["working_set_hash"],
        "local_acceptance_hash": common.canonical_hash(local),
    }
    for key, value in expected.items():
        if str(record.get(key) or "") != str(value or ""):
            raise RuntimeError(f"人工确认已过期（{key}不一致）；请重新审核当前结果")
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description="Bind local acceptance and explicit confirmation to the current incremental run.")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--action", choices=["validate-local", "confirm", "validate-confirmation"], required=True)
    parser.add_argument("--confirmation-text", default="")
    parser.add_argument("--confirmed-by", default="")
    args = parser.parse_args()
    if args.action == "validate-local":
        validate_local_acceptance(args.job)
    elif args.action == "confirm":
        record_confirmation(args.job, args.confirmation_text, args.confirmed_by)
    else:
        validate_confirmation(args.job)
    print(f"REVIEW_GATE_OK action={args.action}")


if __name__ == "__main__":
    main()
