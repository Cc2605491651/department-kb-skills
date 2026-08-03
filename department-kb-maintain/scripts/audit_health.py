#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re

import maintain_common as common


RISK_TERMS = ("冲突", "废止", "替代", "权限", "制度效力", "谁优先", "是否强制")
EXPLICIT_STALE_RE = re.compile(r"废止|已失效|停止使用|已停用|不再适用|有效期(?:至|截止)")
DISPLAY_SEVERITY = {"P0": "需要立即处理", "P1": "建议尽快处理", "P2": "一般提醒"}
DISPLAY_FINDING = {
    "来源孤立": "原文连续未找到",
    "原文疑似失联": "原文暂时未找到",
    "双页面缺失": "知识页不完整",
    "导航孤立": "目录入口缺失",
    "关系孤立": "暂未发现明确关联文档",
    "新冲突": "新发现的内容冲突",
    "新增高风险关系": "新增的关系需要负责人确认",
    "待确认高风险关系": "文档关系需要负责人确认",
}


def display_title(finding: dict, *, related: bool = False) -> str:
    key = "related_title" if related else "title"
    return str(finding.get(key) or "").strip() or "未取得文档名称"


def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def json_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        parsed = json.loads(str(value or "[]"))
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return [str(value)] if value else []


def add(findings: list[dict], *, severity: str, kind: str, source_id: str = "", title: str = "", source_url: str = "", detail: str, action: str, related_id: str = "", related_title: str = "", related_url: str = "", new_issue: bool = True) -> None:
    findings.append({
        "severity": severity,
        "finding_type": kind,
        "source_id": source_id,
        "title": title,
        "source_url": common.sanitize_transient_url(source_url),
        "related_source_id": related_id,
        "related_title": related_title,
        "related_url": common.sanitize_transient_url(related_url),
        "detail": detail,
        "recommended_action": action,
        "new_issue": new_issue,
    })


def audit(job: Path, *, stale_days: int = 180) -> dict:
    job = job.resolve()
    paths = common.state_paths(job)
    plan = common.load_json(paths["plan"], {}) or {}
    rows = common.load_json(job / "01-inventory" / "raw-manifest.json", []) or []
    by_id = {str(row.get("source_id")): row for row in rows if isinstance(row, dict) and row.get("source_id")}
    findings: list[dict] = []

    for change in plan.get("changes") or []:
        if not isinstance(change, dict):
            continue
        kind = str(change.get("change_type") or "")
        if kind not in {"suspected_missing", "source_orphan"}:
            continue
        add(
            findings,
            severity="P0" if kind == "source_orphan" else "P1",
            kind="来源孤立" if kind == "source_orphan" else "原文疑似失联",
            source_id=str(change.get("source_id") or ""), title=str(change.get("title") or ""),
            source_url=str(change.get("source_url") or ""),
            detail=f"连续{int(change.get('missing_count') or 1)}次扫描未发现来源节点。",
            action="确认原文是删除、迁移还是权限不可见；禁止自动删除或判定失效。",
        )

    now = datetime.now(timezone.utc)
    profiles_root = job / "02-extraction-cache" / "semantic" / "source-profiles"
    for source_id, row in by_id.items():
        if row.get("parse_status") != "全文已解析":
            continue
        updated = common.parse_datetime(row.get("update_time"))
        if updated and (now - updated.astimezone(timezone.utc)).days >= max(1, stale_days):
            add(
                findings, severity="P2", kind="长期未复查", source_id=source_id,
                title=str(row.get("file_name") or ""), source_url=str(row.get("source_url") or ""),
                detail=f"来源距今约{(now - updated.astimezone(timezone.utc)).days}天未更新；这不等于已经失效。",
                action="由内容负责人确认仍有效、需要更新或转历史。",
            )
        profile = common.load_json(profiles_root / f"{source_id}.json", {}) or {}
        content = profile.get("content_profile") if isinstance(profile.get("content_profile"), dict) else {}
        signals = [
            *[str(item) for item in content.get("version_signals") or []],
            *[str(item) for item in content.get("time_signals") or []],
            *[str(item) for item in content.get("warnings") or []],
        ]
        matched = [signal for signal in signals if EXPLICIT_STALE_RE.search(signal)]
        if matched:
            add(
                findings, severity="P0", kind="疑似失效或被替代", source_id=source_id,
                title=str(row.get("file_name") or ""), source_url=str(row.get("source_url") or ""),
                detail="；".join(matched[:3]),
                action="由业务负责人确认当前有效版本、失效时间及另一份文档的处理方式。",
            )

    preview_root = job / "本地审核结果（仅供审核）"
    raw_root = preview_root / "01-原文镜像（按原目录）"
    business_root = preview_root / "02-蒸馏结果（按文档类型）"
    for source_id, row in by_id.items():
        if row.get("parse_status") != "全文已解析":
            continue
        raw_pages = list(raw_root.rglob(f"*{source_id}*.md")) if raw_root.exists() else []
        business_pages = list(business_root.rglob(f"*{source_id}*.md")) if business_root.exists() else []
        if len(raw_pages) != 1 or len(business_pages) != 1:
            add(
                findings, severity="P0", kind="双页面缺失", source_id=source_id,
                title=str(row.get("file_name") or ""), source_url=str(row.get("source_url") or ""),
                detail=f"原目录页面={len(raw_pages)}，业务目录页面={len(business_pages)}，预期各1份。",
                action="重新生成页面和目录索引，验收通过后再发布。",
            )
        for page in [*raw_pages, *business_pages]:
            if not any("目录索引" in item.name for item in page.parent.glob("*.md")):
                add(
                    findings, severity="P1", kind="导航孤立", source_id=source_id,
                    title=str(row.get("file_name") or ""), source_url=str(row.get("source_url") or ""),
                    detail=f"页面所在目录缺少目录索引：{page.parent}",
                    action="自动重建该层目录索引并复查页面可达性。",
                )

    ready = load_csv(job / "05-ledgers" / "relation-publication-ready.csv")
    degree: Counter[str] = Counter()
    for relation in ready:
        degree[str(relation.get("source_id") or "")] += 1
        degree[str(relation.get("target_id") or "")] += 1
    for source_id, row in by_id.items():
        if row.get("parse_status") == "全文已解析" and degree[source_id] == 0:
            add(
                findings, severity="P2", kind="关系孤立", source_id=source_id,
                title=str(row.get("file_name") or ""), source_url=str(row.get("source_url") or ""),
                detail="当前没有任何通过发布门槛的文档关系；不代表页面错误或现实中没有关系。",
                action="抽样检查标题引用、业务编号和输入输出线索；证据不足时保持空关系。",
            )

    # The current L3 queue is the business-facing source of truth. Raw
    # verification rows can include superseded classifications and must not be
    # reported as current conflicts. Fall back only for older task packages.
    current_queue = job / "05-ledgers" / "relation-review-queue.csv"
    relation_rows = load_csv(current_queue) if current_queue.exists() else load_csv(job / "05-ledgers" / "relation-verification.csv")
    old_health = common.load_json(paths["health_state"], {}) or {}
    known = set(old_health.get("committed_conflict_signatures") or [])
    current_signatures: set[str] = set()
    for relation in relation_rows:
        review_status = str(relation.get("review_status") or "")
        if review_status in {"已确认", "修改后确认", "已驳回"}:
            continue
        risks = json_list(relation.get("risk_flags"))
        combined = " ".join([
            str(relation.get("review_level") or ""), str(relation.get("l3_reason") or relation.get("verification_reason") or ""),
            str(relation.get("business_relation_name") or relation.get("verified_relation_type") or ""), *risks,
        ])
        if relation.get("review_level") != "L3" and not any(term in combined for term in RISK_TERMS):
            continue
        source_id = str(relation.get("source_id") or "")
        target_id = str(relation.get("target_id") or "")
        source_hash = str((by_id.get(source_id) or {}).get("source_hash") or "")
        target_hash = str((by_id.get(target_id) or {}).get("source_hash") or "")
        relation_name = relation.get("business_relation_name") or relation.get("verified_relation_type", "")
        signature = common.fingerprint(source_id, target_id, source_hash, target_hash, sorted(risks), relation_name)
        current_signatures.add(signature)
        is_new = signature not in known
        is_conflict = "冲突" in combined
        finding_kind = (
            "新冲突" if is_new and is_conflict
            else "新增高风险关系" if is_new
            else "待确认高风险关系"
        )
        add(
            findings, severity="P0", kind=finding_kind,
            source_id=source_id, related_id=target_id,
            title=str(relation.get("source_title") or ""), source_url=str(relation.get("source_url") or ""),
            related_title=str(relation.get("target_title") or ""), related_url=str(relation.get("target_url") or ""),
            detail=str(relation.get("l3_reason") or relation.get("verification_reason") or relation.get("relation_meaning") or "存在需负责人确认的高风险关系"),
            action="确认哪份文档现在有效、分别适用于什么情况，以及另一份文档要修改、停用还是留作历史。",
            new_issue=is_new,
        )

    severity_counts = Counter(row["severity"] for row in findings)
    type_counts = Counter(row["finding_type"] for row in findings)
    report = {
        "schema_version": common.STATE_VERSION,
        "generated_at": common.now_iso(),
        "run_id": plan.get("run_id", ""),
        "findings": findings,
        "counts": {
            "total": len(findings),
            "by_severity": dict(sorted(severity_counts.items())),
            "by_type": dict(sorted(type_counts.items())),
            "new_conflicts": sum(row["finding_type"] == "新冲突" for row in findings),
        },
    }
    common.write_json(paths["health"], report)
    common.write_csv(paths["reports"] / "知识库健康检查清单.csv", findings)
    lines = [
        "# 需要关注的问题", "", f"- 检查时间：{report['generated_at']}",
        f"- 共发现：{len(findings)}条", f"- 需要立即处理：{severity_counts.get('P0', 0)}",
        f"- 建议尽快处理：{severity_counts.get('P1', 0)}", f"- 一般提醒：{severity_counts.get('P2', 0)}",
        f"- 新发现的内容冲突：{report['counts']['new_conflicts']}", "",
        "> 长期未更新、暂时找不到原文或出现“废止”等词，只表示需要人工检查，不代表文档已经失效。", "",
    ]
    for finding in findings:
        level = DISPLAY_SEVERITY.get(str(finding.get("severity") or ""), "需要确认")
        finding_name = DISPLAY_FINDING.get(str(finding.get("finding_type") or ""), str(finding.get("finding_type") or "需要确认"))
        detail_lines = [
            f"## {level}｜{finding_name}｜{display_title(finding)}", "",
            f"- 说明：{finding['detail']}", f"- 下一步：{finding['recommended_action']}",
            f"- 原文：{finding.get('source_url') or '当前不可用'}", "",
        ]
        if finding.get("related_source_id"):
            detail_lines[-1:-1] = [
                f"- 关联文档：{display_title(finding, related=True)}",
                f"- 关联原文：{finding.get('related_url') or '当前不可用'}",
            ]
        lines.extend(detail_lines)
    common.atomic_write(paths["reports"] / "知识库健康检查.md", "\n".join(lines))
    common.write_json(paths["health_state"], {
        "schema_version": common.STATE_VERSION,
        "updated_at": report["generated_at"],
        "committed_conflict_signatures": sorted(known),
        "observed_high_risk_signatures": sorted(current_signatures),
        "observed_run_id": plan.get("run_id", ""),
    })
    print(f"HEALTH_AUDIT_OK findings={len(findings)} p0={severity_counts.get('P0', 0)} new_conflicts={report['counts']['new_conflicts']}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit stale, orphaned and conflicting knowledge pages.")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--stale-days", type=int, default=180)
    args = parser.parse_args()
    audit(args.job, stale_days=args.stale_days)


if __name__ == "__main__":
    main()
