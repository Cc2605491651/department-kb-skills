#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_common as common


def main() -> None:
    parser = argparse.ArgumentParser(description="Record explicit batch acceptance and promote unchanged successful documents to formal status.")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--confirmation-text", required=True)
    parser.add_argument("--confirmed-by", default="")
    args = parser.parse_args()
    job = args.job.resolve()
    confirmation_text = args.confirmation_text.strip()
    if not confirmation_text:
        raise SystemExit("必须保留执行者在AI对话框中的明确确认原话")
    local_acceptance = common.load_json(common.job_paths(job)["reports"] / "local-acceptance.json", {})
    if not isinstance(local_acceptance, dict) or local_acceptance.get("passed") is not True:
        raise SystemExit("本地验收未通过，不能将文档状态改为正式")
    config = common.load_task_config(job)
    confirmed_by = args.confirmed_by.strip() or str(config.get("execution_identity.executed_by", "executed_by", default="") or "").strip()
    if not confirmed_by or confirmed_by.startswith("<"):
        raise SystemExit("必须提供本次蒸馏执行审核人的姓名")
    manifest = common.load_manifest(job)
    success_ids = {
        str(row.get("source_id") or "")
        for row in manifest if row.get("parse_status") in common.SUCCESS_STATUSES
    }
    paths = common.job_paths(job)
    documents: list[dict] = []
    unresolved_owners: list[str] = []
    for source_id in sorted(success_ids):
        profile = common.load_json(paths["source_profiles"] / f"{source_id}.json")
        if not isinstance(profile, dict):
            raise SystemExit(f"缺少成功文档的来源画像：{source_id}")
        owner = str(profile.get("creator_name") or profile.get("owner") or "").strip()
        if not owner or owner == "待补充":
            unresolved_owners.append(source_id)
        content_hash = str(profile.get("source_hash") or "")
        if not content_hash:
            raise SystemExit(f"来源画像缺少内容哈希：{source_id}")
        documents.append({"source_id": source_id, "content_hash": content_hash})
    if unresolved_owners:
        raise SystemExit(f"仍有{len(unresolved_owners)}份成功文档未取得创建者姓名，不能确认整批无问题")
    record = {
        "schema_version": "distillation-acceptance-v1",
        "task_id": str(config.get("task_id", default="") or ""),
        "decision": "confirmed",
        "resulting_status": "正式",
        "confirmed_by": confirmed_by,
        "confirmed_at": common.now_iso(),
        "confirmation_channel": "AI对话框",
        "confirmation_text": confirmation_text,
        "document_count": len(documents),
        "documents": documents,
    }
    common.write_json(paths["ledgers"] / "distillation-acceptance.json", record)
    print(f"DISTILLATION_CONFIRMED documents={len(documents)} status=正式 confirmed_by={confirmed_by}")


if __name__ == "__main__":
    main()
