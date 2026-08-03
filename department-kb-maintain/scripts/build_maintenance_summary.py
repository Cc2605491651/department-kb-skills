#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import quote

import maintain_common as common


SEVERITY_LABELS = {
    "P0": "需要立即处理",
    "P1": "建议尽快处理",
    "P2": "一般提醒",
}

FINDING_LABELS = {
    "来源孤立": "原文连续未找到",
    "原文疑似失联": "原文暂时未找到",
    "双页面缺失": "知识页不完整",
    "导航孤立": "目录入口缺失",
    "关系孤立": "暂未发现明确关联文档",
    "新冲突": "新发现的内容冲突",
    "新增高风险关系": "新增的关系需要负责人确认",
    "待确认高风险关系": "文档关系需要负责人确认",
}


def display_title(row: dict, *, related: bool = False) -> str:
    key = "related_title" if related else "title"
    return str(row.get(key) or "").strip() or "未取得文档名称"


def plain_exclusion_reason(value: object) -> str:
    reason = str(value or "").strip().replace("Raw", "文件收集")
    if not reason or "白名单" in reason or "unsupported" in reason.casefold():
        return "文件类型或内容不符合收集要求，暂时没有处理"
    return reason


def markdown_path(path: Path) -> str:
    return quote(path.resolve().as_posix(), safe="/:")


def page_links(job: Path, source_id: str) -> tuple[str, str]:
    preview = job / "本地审核结果（仅供审核）"
    raw = list((preview / "01-原文镜像（按原目录）").rglob(f"*{source_id}*.md"))
    business = list((preview / "02-蒸馏结果（按文档类型）").rglob(f"*{source_id}*.md"))
    return (
        markdown_path(raw[0]) if len(raw) == 1 else "",
        markdown_path(business[0]) if len(business) == 1 else "",
    )


def build_run_review(job: Path, plan: dict, health: dict, status: str) -> str:
    changes = [row for row in plan.get("changes") or [] if isinstance(row, dict)]
    actionable = [row for row in changes if row.get("change_type") not in {"unchanged", "raw_excluded", "suspected_missing", "source_orphan"}]
    excluded = [row for row in changes if row.get("change_type") == "raw_excluded"]
    missing = [row for row in changes if row.get("change_type") in {"suspected_missing", "source_orphan"}]
    findings = [row for row in health.get("findings") or [] if isinstance(row, dict) and row.get("severity") in {"P0", "P1"}]
    lines = [
        "# 本次需要你检查的内容", "", f"> {status}。这里只列出本次有变化或需要确认的内容。", "",
        "## 本次新建或更新的知识页", "",
    ]
    if not actionable:
        lines.append("- 无")
    for row in actionable:
        source_id = str(row.get("source_id") or "")
        raw, business = page_links(job, source_id)
        links = []
        if raw:
            links.append(f"[原目录页面]({raw})")
        if business:
            links.append(f"[业务目录页面]({business})")
        source_url = common.sanitize_transient_url(row.get("source_url"))
        if source_url:
            links.append(f"[钉钉原文]({source_url})")
        lines.append(f"- {display_title(row)}：{' ｜ '.join(links) or '页面尚未生成'}")
    lines.extend(["", "## 新增但暂时没有处理的文件", ""])
    if not excluded:
        lines.append("- 无")
    for row in excluded:
        source_url = common.sanitize_transient_url(row.get("source_url"))
        source = f"[打开原文]({source_url})" if source_url else "原文链接不可用"
        lines.append(f"- {display_title(row)}：{plain_exclusion_reason(row.get('raw_admission_reason'))}；{source}")
    lines.extend(["", "## 暂时找不到原文的文件", ""])
    if not missing:
        lines.append("- 无")
    for row in missing:
        lines.append(f"- {display_title(row)}：连续{row.get('missing_count') or 1}次没有找到，请确认它是被删除、被移动，还是当前账号没有查看权限。")
    lines.extend(["", "## 需要负责人确认", ""])
    if not findings:
        lines.append("- 无")
    for row in findings:
        source_url = common.sanitize_transient_url(row.get("source_url"))
        source = f"[打开原文]({source_url})" if source_url else "原文链接不可用"
        related_url = common.sanitize_transient_url(row.get("related_url"))
        related = f"[打开关联原文]({related_url})" if related_url else "关联原文链接不可用"
        level = SEVERITY_LABELS.get(str(row.get("severity") or ""), "需要确认")
        finding_name = FINDING_LABELS.get(str(row.get("finding_type") or ""), str(row.get("finding_type") or "需要确认"))
        lines.extend([
            f"### {level}｜{finding_name}｜{display_title(row)}", "",
            f"- 文档一：{display_title(row)}；{source}",
            f"- 文档二：{display_title(row, related=True) if row.get('related_source_id') else '无'}；{related if row.get('related_source_id') else '无'}",
            f"- 发生了什么：{row.get('detail')}",
            f"- 需要确认：{row.get('recommended_action')}",
            "",
        ])
    lines.extend([
        "## 其他提醒", "",
        f"- 一般提醒：{sum(1 for row in health.get('findings') or [] if isinstance(row, dict) and row.get('severity') == 'P2')} 条。这些不影响本次审核，详情见[需要关注的问题]({markdown_path(job / '06-reports' / '知识库健康检查.md')})。", "",
    ])
    return "\n".join(lines)


def build(job: Path, *, mode: str = "local", processed: bool = False, published: bool = False, committed: bool = False) -> dict:
    job = job.resolve()
    paths = common.state_paths(job)
    plan = common.load_json(paths["plan"], {}) or {}
    health = common.load_json(paths["health"], {}) or {}
    acceptance = common.load_json(job / "06-reports" / "local-acceptance.json", {}) or {}
    publish_report = common.load_json(job / "06-reports" / "钉钉发布结果.json", {}) or {}
    readback = common.load_json(job / "06-reports" / "钉钉回读校验结果.json", {}) or {}
    confirmation = common.load_json(paths["root"] / "maintenance-confirmation.json", {}) or {}
    counts = plan.get("counts") if isinstance(plan.get("counts"), dict) else {}
    health_counts = health.get("counts") if isinstance(health.get("counts"), dict) else {}
    severity = health_counts.get("by_severity") if isinstance(health_counts.get("by_severity"), dict) else {}
    p0 = int(severity.get("P0") or 0)
    p1 = int(severity.get("P1") or 0)
    readback_ok = (
        published
        and int(readback.get("failed_documents") or 0) == 0
        and isinstance(readback.get("tree_audit"), dict)
        and readback["tree_audit"].get("passed") is True
    )
    local_ok = acceptance.get("passed") is True if processed else True
    unresolved = p0 + p1
    confirmed = confirmation.get("decision") == "confirmed" and str(confirmation.get("run_id") or "") == str(plan.get("run_id") or "")
    if mode == "dry-run":
        status = "只完成了变化检查，尚未处理发生变化的文档"
    elif not local_ok:
        status = "本地处理没有通过检查"
    elif published and not readback_ok:
        status = "已经上传，但线上内容检查没有全部通过"
    elif published and readback_ok:
        status = "已经上线，并确认线上内容正常"
    elif committed:
        status = "已经确认并保存本次结果，没有上传到钉钉"
    elif confirmed:
        status = "已经确认，等待你决定只保存在本地还是上传到钉钉"
    elif processed:
        status = "本地结果已经生成，等待你检查并确认"
    else:
        status = "检查完成，本次没有需要重新处理的文档"
    remaining = unresolved > 0 or not local_ok or (published and not readback_ok) or (processed and not confirmed and not committed)
    report = {
        "generated_at": common.now_iso(),
        "run_id": plan.get("run_id", ""),
        "status": status,
        "remaining_work": remaining,
        "change_counts": counts,
        "health_counts": health_counts,
        "processed": processed,
        "published": published,
        "readback_passed": readback_ok,
        "baseline_committed": committed,
        "explicitly_confirmed": confirmed,
    }
    labels = {
        "new": "新增", "content_check": "更新时间变化，等待核对内容", "content_changed": "正文变化",
        "file_changed_text_unchanged": "文件有更新，但文字内容没有变化", "metadata_only": "只有文档信息变化", "renamed": "改名", "moved": "移动位置",
        "permission_changed": "权限变化", "restored": "恢复可见", "suspected_missing": "疑似失联",
        "source_orphan": "原文连续未找到", "raw_excluded": "新增文件不符合收集要求", "unchanged": "未变化",
    }
    lines = [
        "# 本次更新结果与下一步", "", f"- 完成时间：{report['generated_at']}",
        f"- 当前情况：{status}", f"- 已保存为下次检查的对比依据：{'是' if committed else '否'}", "",
        "## 本次变化", "",
    ]
    if counts:
        lines.extend(f"- {labels.get(key, key)}：{value}" for key, value in counts.items())
    else:
        lines.append("- 暂无变化统计")
    lines.extend([
        "", "## 需要关注的问题", "", f"- 需要立即处理：{p0}", f"- 建议尽快处理：{p1}",
        f"- 一般提醒：{int(severity.get('P2') or 0)}",
        f"- 新发现的内容冲突：{int(health_counts.get('new_conflicts') or 0)}", "",
        "## 下一步", "",
    ])
    if processed and not confirmed and local_ok:
        lines.extend([
            "- 先查看《01-本次增量审核入口》中的变化文档和待处理问题。",
            "- 没问题时在AI对话框明确说“确认本次增量蒸馏没有问题”，并提供审核人姓名。",
            "- 确认后，再告诉AI是“只保存本地结果”还是“把最终Wiki上线到钉钉”。",
            "- 未确认前，本次结果不会上线，也不会作为下次检查的对比依据。",
        ])
    elif remaining:
        lines.extend([
            "- 当前仍有问题需要处理，不能视为全部完成。", "- 先打开《需要关注的问题》，处理“需要立即处理”和“建议尽快处理”的内容。",
            "- 文档内容有冲突时由业务负责人确认；找不到原文时需确认是删除、移动还是没有查看权限。",
            "- 处理完成后让AI重新检查；没有明确要求前不会自动上线。",
        ])
    elif mode == "dry-run":
        lines.append("- 查看变化清单后，让AI继续处理发生变化的文档。")
    elif confirmed and not published and not committed:
        lines.append("- 告诉AI是“只保存本地结果”，还是“把最终Wiki上线到钉钉”。")
    elif committed and not published:
        lines.append("- 本次结果已经保存；后续需要上线时直接告诉AI即可。")
    elif not processed:
        lines.append("- 本次没有需要重新处理的文档，按计划进行下一次检查即可。")
    else:
        lines.append("- 本次没有未处理的问题，按计划进行下一次检查即可。")
    lines.extend([
        "", "## 结果入口", "",
        f"- [本次变化清单]({markdown_path(paths['reports'] / '增量变化清单.csv')})",
        f"- [需要关注的问题]({markdown_path(paths['reports'] / '知识库健康检查.md')})",
        f"- [查看全部本地结果]({markdown_path(job / '本地审核结果（仅供审核）' / '审核入口.md')})",
        f"- [只看本次有变化的文档]({markdown_path(job / '01-本次增量审核入口.md')})",
    ])
    root = (common.load_json(job / "05-ledgers" / "钉钉发布状态.json", {}) or {}).get("root") or {}
    if root.get("doc_url"):
        label = "线上最终Wiki" if published else "上次线上Wiki（本次未更新）"
        lines.append(f"- [{label}]({root['doc_url']})")
    content = "\n".join(lines) + "\n"
    common.write_json(paths["reports"] / "增量维护结果与下一步.json", report)
    common.atomic_write(paths["reports"] / "增量维护结果与下一步.md", content)
    common.atomic_write(job / "00-增量结果与下一步.md", content)
    common.atomic_write(job / "01-本次增量审核入口.md", build_run_review(job, plan, health, status))
    print(f"MAINTENANCE_SUMMARY_OK status={status} remaining={remaining}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the user-facing incremental maintenance summary.")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--mode", choices=["dry-run", "local", "confirmed", "published"], default="local")
    parser.add_argument("--processed", action="store_true")
    parser.add_argument("--published", action="store_true")
    parser.add_argument("--committed", action="store_true")
    args = parser.parse_args()
    build(args.job, mode=args.mode, processed=args.processed, published=args.published, committed=args.committed)


if __name__ == "__main__":
    main()
