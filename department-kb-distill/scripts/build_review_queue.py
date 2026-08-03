#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_common as common


FIELDS = [
    "relation_id", "source_id", "source_title", "source_url", "source_creator_uid", "source_creator_name",
    "target_id", "target_title", "target_url", "target_creator_uid", "target_creator_name",
    "proposed_type", "business_relation_name", "relation_type_explanation", "direction", "relation_meaning", "l3_reason",
    "confirmation_question", "risk_flags", "rule_ids", "local_evidence", "codex_evidence",
    "risk_level", "review_level", "reviewer", "review_status", "review_comment", "modified_type",
    "confirmed_at", "published_at", "readback_status", "archive_status", "re_review_at",
    "review_input_hash", "queue_updated_at",
]
DECISION_FIELDS = {
    "reviewer", "review_status", "review_comment", "modified_type", "confirmed_at", "published_at",
    "readback_status", "archive_status", "re_review_at",
}
VALID_STATUSES = {"待确认", "已确认", "已驳回", "修改后确认", "暂缓", "已发布", "已归档", "待复审"}
SAMPLE_FIELDS = [
    "relation_id", "source_id", "source_title", "source_url", "target_id", "target_title", "target_url",
    "verified_relation_type", "relation_type_explanation", "direction", "relation_meaning", "source_evidence", "target_evidence",
    "confidence", "review_level", "rule_ids", "risk_flags", "sample_rate", "sample_status", "sampler",
    "sample_comment", "sampled_at", "queue_updated_at",
]
SAMPLE_DECISION_FIELDS = {"sample_status", "sampler", "sample_comment", "sampled_at"}


def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def load_creator_map(job: Path) -> dict[str, dict]:
    rows = load_csv(job / "05-ledgers" / "document-creators.csv")
    if not rows:
        rows = common.load_manifest(job)
    return {row.get("source_id", ""): row for row in rows if row.get("source_id")}


def default_reviewer(job: Path) -> str:
    config = common.load_task_config(job)
    return str(config.get("execution_identity.executed_by", "executed_by", default="本次AI任务发起人（待补充）"))


def l2_sample_rate(job: Path) -> float:
    config = common.load_task_config(job)
    try:
        return min(1.0, max(0.0, float(config.get("relation_processing.l2_sample_rate", "l2_sample_rate", default=0.2))))
    except (TypeError, ValueError):
        return 0.2


def confirmation_question(relation: dict) -> str:
    reason = str(relation.get("verification_reason") or "").strip()
    risks = str(relation.get("risk_flags") or "")
    combined = f"{risks} {reason}"
    if "冲突" in combined:
        try:
            source_evidence = json.loads(relation.get("source_evidence") or "[]")
            target_evidence = json.loads(relation.get("target_evidence") or "[]")
        except json.JSONDecodeError:
            source_evidence, target_evidence = [], []
        codes = set(re.findall(r"(?<![A-Z0-9])([A-Z]{1,10}\d{1,6})(?![A-Z0-9])", combined))
        source_conflict = next((item for item in source_evidence if any(code in str(item) for code in codes)), "")
        target_conflict = next((item for item in target_evidence if any(code in str(item) for code in codes)), "")
        if source_conflict and target_conflict:
            return f"来源文档证据：{source_conflict}；关联文档证据：{target_conflict}。请确认哪一条是当前有效口径，并指定另一份文档应修订、标记失效还是保留为历史。"
        return f"请确认冲突事项最终以哪份文档为准，并指定另一份文档应修订、标记失效还是保留为历史。需要裁定的具体问题：{reason}"
    if any(term in combined for term in ["废止", "替代", "版本", "效力"]):
        return f"请确认哪份文档是当前有效依据、另一份是否已被替代或废止，并明确生效范围。需要裁定的具体问题：{reason}"
    if any(term in combined for term in ["权限", "泄露", "ACL"]):
        return f"请确认该关系是否允许展示、哪些身份可以看到，并确保不会暴露受限文档的存在或内容。需要裁定的具体问题：{reason}"
    return f"请确认该关系的业务含义、适用范围和当前有效性，并写明最终以哪份文档为准。需要裁定的具体问题：{reason or '详见双方证据'}"


def make_row(relation: dict, reviewer: str, creators: dict[str, dict]) -> dict:
    codex_evidence = json.dumps({
        "source": json.loads(relation.get("source_evidence") or "[]"),
        "target": json.loads(relation.get("target_evidence") or "[]"),
        "reason": relation.get("verification_reason", ""),
        "confidence": relation.get("confidence", ""),
        "coverage_mode": relation.get("coverage_mode", ""),
    }, ensure_ascii=False)
    source_creator = creators.get(relation["source_id"], {})
    target_creator = creators.get(relation["target_id"], {})
    base = {
        "relation_id": relation["relation_id"], "source_id": relation["source_id"],
        "source_title": relation.get("source_title", ""), "source_url": relation.get("source_url", ""),
        "source_creator_uid": source_creator.get("creator_uid", ""),
        "source_creator_name": source_creator.get("creator_name") or "钉钉未返回创建者（待补充）",
        "target_id": relation["target_id"], "target_title": relation.get("target_title", ""),
        "target_url": relation.get("target_url", ""), "target_creator_uid": target_creator.get("creator_uid", ""),
        "target_creator_name": target_creator.get("creator_name") or "钉钉未返回创建者（待补充）",
        "proposed_type": relation.get("verified_relation_type", ""),
        "business_relation_name": common.business_relation_name(relation.get("verified_relation_type", "")),
        "relation_type_explanation": common.relation_type_explanation(relation.get("verified_relation_type", "")),
        "direction": relation.get("direction", ""), "relation_meaning": relation.get("relation_meaning", ""),
        "l3_reason": relation.get("verification_reason", ""),
        "confirmation_question": confirmation_question(relation), "risk_flags": relation.get("risk_flags", "[]"),
        "rule_ids": relation.get("rule_ids", "[]"), "local_evidence": relation.get("local_evidence", "{}"),
        "codex_evidence": codex_evidence, "risk_level": "高" if relation.get("review_level") == "L3" else "中",
        "review_level": relation.get("review_level", "L2"), "reviewer": reviewer, "review_status": "待确认",
        "review_comment": "", "modified_type": "", "confirmed_at": "", "published_at": "",
        "readback_status": "待发布", "archive_status": "未归档", "re_review_at": "",
    }
    evidence_hash_fields = {key: base[key] for key in base if key not in DECISION_FIELDS}
    base["review_input_hash"] = common.sha256_text(json.dumps(evidence_hash_fields, ensure_ascii=False, sort_keys=True))
    base["queue_updated_at"] = common.now_iso()
    return base


def merge_decisions(new_rows: list[dict], old_rows: list[dict]) -> list[dict]:
    old_by_id = {row.get("relation_id", ""): row for row in old_rows}
    for row in new_rows:
        old = old_by_id.get(row["relation_id"])
        if not old:
            continue
        old_status = old.get("review_status", "")
        if old.get("review_input_hash") == row.get("review_input_hash"):
            for field in DECISION_FIELDS:
                if old.get(field, ""):
                    row[field] = old[field]
            if row.get("review_status") not in VALID_STATUSES:
                row["review_status"] = "待确认"
        elif old_status in {"已确认", "修改后确认", "已发布", "已归档"}:
            row["review_status"] = "待复审"
            row["review_comment"] = "来源内容或候选证据已变化，原确认结论需复审。"
            row["re_review_at"] = common.now_iso()
    return new_rows


def markdown(rows: list[dict]) -> str:
    lines = [
        "# L3 高风险关系确认清单", "",
        "本清单只保留业务负责人确认所需信息；规则编号、风险分级和双方证据保存在后台关系台账。", "",
    ]
    for index, row in enumerate(rows, start=1):
        source = f"[{row['source_title']}]({row['source_url']})" if row.get("source_url") else row["source_title"]
        target = f"[{row['target_title']}]({row['target_url']})" if row.get("target_url") else row["target_title"]
        lines.extend([
            f"## {index}. {row['source_title']} ↔ {row['target_title']}", "",
            "| 文档 | 创建者 | 原文链接 |", "|---|---|---|",
            f"| {row['source_title']} | {row['source_creator_name']} | {source} |",
            f"| {row['target_title']} | {row['target_creator_name']} | {target} |", "",
            f"- **存在什么关系**：{row['business_relation_name']}",
            f"- **这段关系是什么意思**：{row['relation_meaning']}",
            f"- **负责人需要确认什么**：{row['confirmation_question']}", "",
        ])
    lines.extend(["", "确认后处理：已确认/修改后确认 → 发布 → 回读 → 关系台账写回 → 确认记录归档。已驳回不发布，暂缓保留候选。", ""])
    return "\n".join(lines)


def select_l2_sample(rows: list[dict], rate: float) -> list[dict]:
    if not rows or rate <= 0:
        return []
    count = max(1, math.ceil(len(rows) * rate))
    return sorted(rows, key=lambda row: hashlib.sha256(row["relation_id"].encode()).hexdigest())[:count]


def make_sample_row(relation: dict, reviewer: str, rate: float, old: dict | None = None) -> dict:
    row = {
        "relation_id": relation["relation_id"], "source_id": relation["source_id"],
        "source_title": relation.get("source_title", ""), "source_url": relation.get("source_url", ""),
        "target_id": relation["target_id"], "target_title": relation.get("target_title", ""),
        "target_url": relation.get("target_url", ""), "verified_relation_type": relation.get("verified_relation_type", ""),
        "relation_type_explanation": common.relation_type_explanation(relation.get("verified_relation_type", "")),
        "direction": relation.get("direction", ""), "relation_meaning": relation.get("relation_meaning", ""),
        "source_evidence": relation.get("source_evidence", "[]"), "target_evidence": relation.get("target_evidence", "[]"),
        "confidence": relation.get("confidence", ""), "review_level": "L2", "rule_ids": relation.get("rule_ids", "[]"),
        "risk_flags": relation.get("risk_flags", "[]"), "sample_rate": rate, "sample_status": "待抽查",
        "sampler": reviewer, "sample_comment": "", "sampled_at": "", "queue_updated_at": common.now_iso(),
    }
    if old:
        for field in SAMPLE_DECISION_FIELDS:
            if old.get(field):
                row[field] = old[field]
    return row


def sampling_markdown(rows: list[dict], pool_size: int, rate: float) -> str:
    lines = [
        "# L2 自动通过关系抽样清单", "",
        f"- L2 自动通过总数：{pool_size}", f"- 抽样比例：{rate:.0%}", f"- 本次抽中：{len(rows)}", "",
        "抽查不阻断 L2 关系进入 `relations`。如发现错误，将 `sample_status` 改为“抽查需修正”并写明原因，该关系应撤回并升级复审。", "",
        "| 抽查状态 | 来源 | 系统关系标签 | 标签通俗解释 | 目标 | 方向 | 本条关系具体含义 |", "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        source = f"[{row['source_title']}]({row['source_url']})" if row.get("source_url") else row["source_title"]
        target = f"[{row['target_title']}]({row['target_url']})" if row.get("target_url") else row["target_title"]
        meaning = str(row.get("relation_meaning") or "").replace("|", "｜").replace("\n", " ")
        type_explanation = str(row.get("relation_type_explanation") or "").replace("|", "｜").replace("\n", " ")
        lines.append(f"| {row['sample_status']} | {source} | {row['verified_relation_type']} | {type_explanation} | {target} | {row['direction']} | {meaning} |")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the business relation confirmation queue.")
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()
    paths = common.job_paths(args.job)
    verification = load_csv(paths["ledgers"] / "relation-verification.csv")
    queue_path = paths["ledgers"] / "relation-review-queue.csv"
    old_rows = load_csv(queue_path)
    reviewer = default_reviewer(args.job)
    creators = load_creator_map(args.job)
    selected: list[dict] = []
    for row in verification:
        if row.get("verification_status") not in {"confirmed", "needs_review"}:
            continue
        if row.get("review_level") == "L3" or row.get("verification_status") == "needs_review":
            selected.append({**row, "review_level": "L3"})
    rows = merge_decisions([make_row(row, reviewer, creators) for row in selected], old_rows)
    common.write_csv(queue_path, rows, FIELDS)
    common.atomic_write(paths["ledgers"] / "relation-review-queue.jsonl", "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    l3_markdown = markdown(rows)
    common.atomic_write(paths["reports"] / "L3高风险关系确认清单.md", l3_markdown)
    common.atomic_write(paths["reports"] / "关键关系业务确认清单.md", "# 清单已更名\n\nL2 现已自动通过并事后抽样；请查看《L3高风险关系确认清单.md》。\n")

    rate = l2_sample_rate(args.job)
    l2_pool = [row for row in verification if row.get("review_level") == "L2" and row.get("verification_status") == "confirmed"]
    sampled = select_l2_sample(l2_pool, rate)
    sample_path = paths["ledgers"] / "relation-sampling-queue.csv"
    old_sample = {row.get("relation_id", ""): row for row in load_csv(sample_path)}
    sample_rows = [make_sample_row(row, reviewer, rate, old_sample.get(row["relation_id"])) for row in sampled]
    common.write_csv(sample_path, sample_rows, SAMPLE_FIELDS)
    common.atomic_write(paths["ledgers"] / "relation-sampling-queue.jsonl", "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in sample_rows))
    common.atomic_write(paths["reports"] / "L2自动通过关系抽样清单.md", sampling_markdown(sample_rows, len(l2_pool), rate))
    print(f"REVIEW_QUEUE_OK l3_rows={len(rows)} l3_pending={sum(row['review_status'] in {'待确认', '待复审'} for row in rows)} l2_pool={len(l2_pool)} l2_sample={len(sample_rows)}")


if __name__ == "__main__":
    main()
