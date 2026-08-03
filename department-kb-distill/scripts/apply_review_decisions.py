#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_common as common


def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply reviewed relation decisions to publication gates.")
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()
    paths = common.job_paths(args.job)
    relations = load_csv(paths["ledgers"] / "relation-verification.csv")
    queue = load_csv(paths["ledgers"] / "relation-review-queue.csv")
    decisions = {row.get("relation_id", ""): row for row in queue}
    fields = list(relations[0].keys()) + [
        "business_review_status", "business_review_comment", "business_reviewer", "business_confirmed_at",
        "publication_gate", "final_relation_type", "archive_status",
    ] if relations else []
    output: list[dict] = []
    ready: list[dict] = []
    archived: list[dict] = []
    for relation in relations:
        level = relation.get("review_level", "")
        verification_status = relation.get("verification_status", "")
        decision = decisions.get(relation["relation_id"], {})
        status = decision.get("review_status", "")
        if verification_status == "rejected":
            gate = "rejected"
            final_type = ""
            business_status = "Codex核验驳回"
        elif level in {"L0", "L1"} and verification_status == "confirmed":
            gate = "ready"
            final_type = relation.get("verified_relation_type", "")
            business_status = "无需逐条业务确认"
        elif level == "L2" and verification_status == "confirmed":
            gate = "ready"
            final_type = relation.get("verified_relation_type", "")
            business_status = "L2自动通过，待事后抽样"
        elif status in {"已确认", "修改后确认", "已发布", "已归档"}:
            gate = "ready"
            final_type = decision.get("modified_type") if status == "修改后确认" and decision.get("modified_type") else relation.get("verified_relation_type", "")
            business_status = status
        elif status == "已驳回":
            gate = "rejected"
            final_type = ""
            business_status = status
        elif status == "暂缓":
            gate = "hold"
            final_type = ""
            business_status = status
        else:
            gate = "pending_review"
            final_type = ""
            business_status = status or "待确认"
        merged = {
            **relation,
            "business_review_status": business_status,
            "business_review_comment": decision.get("review_comment", ""),
            "business_reviewer": decision.get("reviewer", ""),
            "business_confirmed_at": decision.get("confirmed_at", ""),
            "publication_gate": gate,
            "final_relation_type": final_type,
            "archive_status": decision.get("archive_status", "未归档"),
        }
        output.append(merged)
        if gate == "ready":
            ready.append(merged)
        if decision.get("archive_status") == "已归档":
            archived.append({**decision, "archived_snapshot_at": common.now_iso()})
    common.write_csv(paths["ledgers"] / "relation-ledger.csv", output, fields)
    common.write_csv(paths["ledgers"] / "relation-publication-ready.csv", ready, fields)
    archive_fields = list(archived[0].keys()) if archived else ["relation_id", "archived_snapshot_at"]
    common.write_csv(paths["ledgers"] / "relation-review-archive.csv", archived, archive_fields)
    print(f"REVIEW_DECISIONS_APPLIED total={len(output)} ready={len(ready)} pending={sum(row['publication_gate'] == 'pending_review' for row in output)} rejected={sum(row['publication_gate'] == 'rejected' for row in output)} archived={len(archived)}")


if __name__ == "__main__":
    main()
