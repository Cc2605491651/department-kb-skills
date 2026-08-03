#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import maintain_common as common
from audit_health import audit
from build_incremental_plan import build as build_plan
from build_maintenance_summary import build as build_summary
from commit_applied_state import commit
from finalize_incremental_plan import finalize
from initialize_baseline import initialize
from prepare_working_set import prepare
from review_gate import record_confirmation, record_local_acceptance, validate_confirmation, validate_local_acceptance
from snapshot_sources import scan


def ensure_config(job: Path) -> None:
    path = job / "00-config" / "maintenance-config.yaml"
    if not path.exists():
        raise RuntimeError(f"缺少维护配置：{path}；请从Skill assets复制模板")
    if not common.bool_value(common.config_value(job, "maintenance.enabled", False)):
        raise RuntimeError("maintenance-config.yaml未启用maintenance.enabled")
    if int(common.config_value(job, "maintenance.contract_version", 0) or 0) != 1:
        raise RuntimeError("maintenance.contract_version必须为1")
    for gate in ("separate_incremental_job_required", "publish_requires_explicit_stage", "require_explicit_confirmation", "require_current_run_acceptance"):
        if not common.bool_value(common.config_value(job, f"maintenance.{gate}", False)):
            raise RuntimeError(f"maintenance.{gate}必须为true")
    if not (job / "00-config" / "baseline-reference.json").exists():
        raise RuntimeError("缺少baseline-reference.json；请用create_incremental_job.py从已验收全量任务创建独立增量目录")
    if not (job / "01-inventory" / "raw-manifest.json").exists():
        raise RuntimeError("任务尚未完成首次全量盘点；请先使用department-kb-distill")


def base_pipeline(base: Path, job: Path, stage: str, *, workers: int = 4, source_ids: list[str] | None = None, model: str = "", publish_workers: int = 30, last_scope: bool = False) -> None:
    args = ["--job", str(job), "--stage", stage]
    if stage in {"extract", "semantic", "verify"}:
        args.extend(["--workers", str(workers)])
    if model and stage in {"semantic", "verify"}:
        args.extend(["--model", model])
    for source_id in source_ids or []:
        args.extend(["--source-id", source_id])
    if stage in {"publish", "readback"}:
        args.extend(["--publish-workers", str(max(1, min(publish_workers, 30)))])
    if last_scope and stage == "readback":
        args.append("--last-scope")
    common.run_base(base, "run_pipeline.py", args, timeout=24 * 3600)


def process_changes(job: Path, base: Path, *, workers: int, model: str) -> bool:
    paths = common.state_paths(job)
    plan = common.load_json(paths["plan"], {}) or {}
    actions = plan.get("actions") if isinstance(plan.get("actions"), dict) else {}
    processing_ids = [str(value) for value in actions.get("processing_source_ids") or []]
    if not processing_ids:
        print("INCREMENTAL_APPLY_SKIPPED no_actionable_source_changes")
        return False
    prepare(job)
    base_pipeline(base, job, "owners", workers=workers)
    common.run_base(base, "preflight.py", ["--job", str(job), "--stage", "admission"])
    common.run_base(base, "apply_raw_admission.py", [
        "--job", str(job), "--force", "--replace-current-snapshot",
    ])
    extract_ids = [str(value) for value in actions.get("extract_source_ids") or []]
    if extract_ids:
        base_pipeline(base, job, "extract", workers=workers, source_ids=extract_ids)
    plan = finalize(job)
    actions = plan.get("actions") if isinstance(plan.get("actions"), dict) else {}
    semantic_ids = [str(value) for value in actions.get("semantic_source_ids") or []]
    # A path/permission-only change still needs source-profile refresh. Content profiles hit cache.
    base_pipeline(base, job, "semantic", workers=workers, source_ids=semantic_ids or processing_ids, model=model)
    for stage in ("candidates", "verify", "review", "apply", "preview", "validate"):
        base_pipeline(base, job, stage, workers=workers, model=model)
    record_local_acceptance(job, processing_ids)
    print(f"INCREMENTAL_APPLY_OK sources={len(processing_ids)} extracted={len(extract_ids)}")
    return True


def publication_allowed(job: Path) -> bool:
    return (
        common.bool_value(common.task_value(job, "publishing.enabled", False))
        and bool(str(common.task_value(job, "publishing.target_folder_url", "") or "").strip())
    )


def confirm_current(job: Path, base: Path, confirmation_text: str, confirmed_by: str, *, workers: int) -> dict:
    local = validate_local_acceptance(job)
    common.run_base(base, "run_pipeline.py", [
        "--job", str(job), "--stage", "confirm",
        "--confirmation-text", confirmation_text,
        "--confirmed-by", confirmed_by,
        "--workers", str(workers),
    ], timeout=24 * 3600)
    # The full engine promotes candidate pages to formal and reruns validation.
    # Rebind the acceptance hash to those newly rendered pages, then store the
    # human confirmation against exactly this run and working set.
    record_local_acceptance(job, [str(value) for value in local.get("processed_source_ids") or []])
    return record_confirmation(job, confirmation_text, confirmed_by)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run incremental maintenance for an already distilled department knowledge base.")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--stage", choices=["preflight", "baseline", "scan", "plan", "apply", "health", "confirm", "commit", "publish", "all"], required=True)
    parser.add_argument("--base-skill-root", default="")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--publish-workers", type=int, default=30)
    parser.add_argument("--model", default="")
    parser.add_argument("--permission-scan", choices=["off", "all"], default="")
    parser.add_argument("--hash-audit", choices=["off", "all"], default="")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--require-readback", action="store_true")
    parser.add_argument("--confirmation-text", default="")
    parser.add_argument("--confirmed-by", default="")
    args = parser.parse_args()
    job = args.job.resolve()
    ensure_config(job)
    base = common.find_base_skill(args.base_skill_root)
    if args.stage == "preflight":
        print(f"MAINTENANCE_PREFLIGHT_OK job={job} base={base}")
        return
    permission_mode = args.permission_scan or str(common.config_value(job, "maintenance.permission_scan", "off") or "off")
    if permission_mode not in {"off", "all"}:
        raise RuntimeError("maintenance.permission_scan只允许off或all")
    hash_audit = args.hash_audit or str(common.config_value(job, "maintenance.hash_audit", "off") or "off")
    if hash_audit not in {"off", "all"}:
        raise RuntimeError("maintenance.hash_audit只允许off或all")
    missing_runs = int(common.config_value(job, "maintenance.missing_confirm_runs", 2) or 2)
    stale_days = int(common.config_value(job, "maintenance.stale_review_days", 180) or 180)
    with common.task_lock(job):
        if args.stage == "baseline":
            initialize(job)
            return
        if args.stage == "scan":
            scan(job, base, workers=args.workers, permission_mode=permission_mode)
            return
        if args.stage == "plan":
            build_plan(job, missing_confirm_runs=missing_runs, force_hash_audit=hash_audit == "all")
            return
        if args.stage == "apply":
            processed = process_changes(job, base, workers=args.workers, model=args.model)
            audit(job, stale_days=stale_days)
            build_summary(job, mode="local", processed=processed)
            return
        if args.stage == "health":
            audit(job, stale_days=stale_days)
            build_summary(job, mode="local", processed=False)
            return
        if args.stage == "confirm":
            if not args.confirmation_text.strip() or not args.confirmed_by.strip():
                raise RuntimeError("confirm必须传入--confirmation-text和--confirmed-by")
            confirm_current(job, base, args.confirmation_text, args.confirmed_by, workers=args.workers)
            audit(job, stale_days=stale_days)
            build_summary(job, mode="confirmed", processed=True)
            return
        if args.stage == "commit":
            validate_confirmation(job)
            commit(job, require_readback=args.require_readback)
            build_summary(job, mode="published" if args.require_readback else "local", processed=True, published=args.require_readback, committed=True)
            return
        if args.stage == "publish":
            validate_confirmation(job)
            if not publication_allowed(job):
                raise RuntimeError("任务未在task-config.yaml启用发布或未锁定目标文件夹")
            base_pipeline(base, job, "publish", publish_workers=args.publish_workers)
            base_pipeline(base, job, "readback", publish_workers=args.publish_workers, last_scope=True)
            commit(job, require_readback=True)
            audit(job, stale_days=stale_days)
            build_summary(job, mode="published", processed=True, published=True, committed=True)
            return

        if args.publish:
            raise RuntimeError("--stage all只生成本地结果；请审核并执行confirm后，再单独执行--stage publish")
        if not common.state_paths(job)["applied"].exists():
            initialize(job)
        scan(job, base, workers=args.workers, permission_mode=permission_mode)
        plan = build_plan(job, missing_confirm_runs=missing_runs, force_hash_audit=hash_audit == "all")
        if args.dry_run:
            audit(job, stale_days=stale_days)
            build_summary(job, mode="dry-run", processed=False)
            return
        processed = process_changes(job, base, workers=args.workers, model=args.model)
        audit(job, stale_days=stale_days)
        # `all` is intentionally local-only. The successful baseline remains
        # untouched until the user explicitly confirms this exact run.
        build_summary(job, mode="local", processed=processed, published=False, committed=False)


if __name__ == "__main__":
    main()
