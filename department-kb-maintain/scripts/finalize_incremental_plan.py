#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import maintain_common as common


def finalize(job: Path) -> dict:
    job = job.resolve()
    paths = common.state_paths(job)
    plan = common.load_json(paths["plan"], {})
    applied = common.load_json(paths["applied"], {}) or {}
    rows = common.load_json(job / "01-inventory" / "raw-manifest.json", [])
    excluded = common.load_json(job / "01-inventory" / "raw-admission" / "excluded-manifest.json", [])
    current = {str(row.get("source_id")): row for row in rows if isinstance(row, dict) and row.get("source_id")}
    excluded_by_id = {
        str(row.get("source_id")): row
        for row in excluded or [] if isinstance(row, dict) and row.get("source_id")
    }
    excluded_ids = set(excluded_by_id)
    old = applied.get("documents") if isinstance(applied.get("documents"), dict) else {}
    for change in plan.get("changes") or []:
        if not isinstance(change, dict):
            continue
        source_id = str(change.get("source_id") or "")
        flags = list(change.get("change_flags") or [])
        if source_id in excluded_ids:
            flags = [flag for flag in flags if flag != "content_check"]
            if "raw_excluded" not in flags:
                flags.append("raw_excluded")
            change["change_type"] = "raw_excluded"
            change["final_status"] = "经Raw标准排除"
            excluded_row = excluded_by_id[source_id]
            change["raw_admission_reason"] = excluded_row.get("admission_reason", "不符合Raw格式白名单")
            change["responsible_action"] = "按排除原因整理成白名单格式后重新提交；本轮不进入蒸馏"
            continue
        row = current.get(source_id)
        if not row or "content_check" not in flags:
            continue
        previous_hash = str((old.get(source_id) or {}).get("source_hash") or "")
        current_hash = str(row.get("source_hash") or "")
        previous_text_hash = str((old.get(source_id) or {}).get("extracted_hash") or "")
        current_text_hash = str(row.get("extracted_hash") or "")
        flags = [flag for flag in flags if flag != "content_check"]
        if current_text_hash and previous_text_hash and current_text_hash != previous_text_hash:
            flags.append("content_changed")
            change["change_type"] = "content_changed"
            change["final_status"] = "正文已变化，重新蒸馏"
        elif current_text_hash and previous_text_hash == current_text_hash and current_hash != previous_hash:
            flags.append("file_changed_text_unchanged")
            change["change_type"] = "file_changed_text_unchanged"
            change["final_status"] = "源文件变化但提取文字一致，复用AI画像并刷新来源信息"
        elif current_text_hash and previous_text_hash == current_text_hash:
            flags.append("metadata_only")
            change["change_type"] = "metadata_only"
            change["final_status"] = "提取文字哈希未变化，仅刷新来源信息"
        elif current_hash and previous_hash and current_hash != previous_hash:
            flags.append("content_changed")
            change["change_type"] = "content_changed"
            change["final_status"] = "源文件哈希变化且无可比较文字哈希，按正文变化处理"
        elif current_hash and previous_hash == current_hash:
            flags.append("metadata_only")
            change["change_type"] = "metadata_only"
            change["final_status"] = "源文件哈希未变化，仅刷新来源信息"
        else:
            flags.append("content_unresolved")
            change["change_type"] = "content_unresolved"
            change["final_status"] = "未取得可比较正文哈希"
        change["change_flags"] = list(dict.fromkeys(flags))
        change["current_source_hash"] = current_hash
        change["previous_extracted_hash"] = previous_text_hash
        change["current_extracted_hash"] = current_text_hash
    counts = Counter(str(row.get("change_type") or "unknown") for row in plan.get("changes") or [])
    plan["counts"] = dict(sorted(counts.items()))
    plan["finalized_at"] = common.now_iso()
    plan["phase"] = "post_extraction"
    common.write_json(paths["plan"], plan)
    common.write_json(paths["reports"] / "增量变化清单.json", plan)
    report_rows = [{**row, "change_flags": ",".join(row.get("change_flags") or [])} for row in plan.get("changes") or []]
    common.write_csv(paths["reports"] / "增量变化清单.csv", report_rows)
    print(f"INCREMENTAL_PLAN_FINALIZED counts={dict(counts)}")
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description="Finalize preliminary content changes after extraction and hashing.")
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()
    finalize(args.job)


if __name__ == "__main__":
    main()
