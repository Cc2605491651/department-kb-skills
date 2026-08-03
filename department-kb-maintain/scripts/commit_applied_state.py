#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import maintain_common as common
from review_gate import validate_confirmation


def commit(job: Path, *, require_readback: bool = False) -> dict:
    job = job.resolve()
    paths = common.state_paths(job)
    confirmation = validate_confirmation(job)
    if require_readback:
        publish_report = common.load_json(job / "06-reports" / "钉钉发布结果.json", {}) or {}
        readback = common.load_json(job / "06-reports" / "钉钉回读校验结果.json", {}) or {}
        tree = readback.get("tree_audit") if isinstance(readback.get("tree_audit"), dict) else {}
        if int(publish_report.get("failed_documents") or 0) != 0:
            raise RuntimeError("本轮发布存在失败文档，禁止提交增量成功状态")
        if int(readback.get("failed_documents") or 0) != 0 or tree.get("passed") is not True:
            raise RuntimeError("发布任务的回读或远程目录树验收未通过，禁止提交增量成功状态")
        confirmed_at = common.parse_datetime(confirmation.get("confirmed_at"))
        published_at = common.parse_datetime(publish_report.get("generated_at"))
        readback_at = common.parse_datetime(readback.get("generated_at"))
        if not confirmed_at or not published_at or not readback_at or published_at < confirmed_at or readback_at < confirmed_at:
            raise RuntimeError("发布或回读报告早于本轮确认，疑似旧报告，禁止提交成功状态")
    plan = common.load_json(paths["plan"], {}) or {}
    old_state = common.load_json(paths["applied"], {}) or {}
    observation = common.load_json(paths["observation"], {}) or {}
    rows = common.load_json(job / "01-inventory" / "raw-manifest.json", []) or []
    now = common.now_iso()
    documents = {
        str(row.get("source_id")): common.document_state(row, observed_at=now)
        for row in rows if isinstance(row, dict) and row.get("source_id")
    }
    old_documents = old_state.get("documents") if isinstance(old_state.get("documents"), dict) else {}
    observed_states = observation.get("documents") if isinstance(observation.get("documents"), dict) else {}
    for source_id, old in old_documents.items():
        if source_id in documents or not isinstance(old, dict):
            continue
        missing = observed_states.get(source_id) if isinstance(observed_states.get(source_id), dict) else {}
        retained = dict(old)
        retained["missing_count"] = int(missing.get("missing_count") or retained.get("missing_count") or 0)
        retained["last_seen_at"] = missing.get("last_seen_at") or retained.get("last_seen_at") or ""
        retained["source_presence"] = "missing_pending_confirmation"
        documents[source_id] = retained
    state = {
        "schema_version": common.STATE_VERSION,
        "established_at": old_state.get("established_at") or now,
        "updated_at": now,
        "task_id": common.task_value(job, "task_id", ""),
        "workspace_id": common.task_value(job, "source.workspace_id", ""),
        "documents": documents,
        "last_successful_run": {
            "run_id": plan.get("run_id", ""),
            "finished_at": now,
            "local_acceptance": True,
            "remote_readback_required": require_readback,
            "remote_readback_passed": require_readback,
            "change_counts": plan.get("counts") or {},
            "confirmed_by": confirmation.get("confirmed_by", ""),
            "confirmed_at": confirmation.get("confirmed_at", ""),
        },
    }
    common.write_json(paths["applied"], state)
    common.write_json(paths["root"] / "applied-history" / f"{plan.get('run_id') or common.run_id()}.json", state)
    common.append_jsonl(paths["root"] / "maintenance-history.jsonl", {
        "event": "applied_state_committed", "at": now, "run_id": plan.get("run_id", ""),
        "documents": len(documents), "require_readback": require_readback,
        "confirmed_by": confirmation.get("confirmed_by", ""),
    })
    health_state = common.load_json(paths["health_state"], {}) or {}
    observed_signatures = health_state.get("observed_high_risk_signatures")
    if isinstance(observed_signatures, list):
        health_state["committed_conflict_signatures"] = sorted({str(value) for value in observed_signatures})
        health_state["committed_at"] = now
        health_state["committed_run_id"] = plan.get("run_id", "")
        common.write_json(paths["health_state"], health_state)
    print(f"APPLIED_STATE_OK documents={len(documents)} readback_required={require_readback}")
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description="Commit the last successful incremental distillation state.")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--require-readback", action="store_true")
    args = parser.parse_args()
    commit(args.job, require_readback=args.require_readback)


if __name__ == "__main__":
    main()
