#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import re
import sys
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_common as common


SKILL_ROOT = Path(__file__).resolve().parent.parent
ASSETS = SKILL_ROOT / "assets"
MATRIX_FIELDS = (
    "稳定ID", "文档标题", "创建者", "原文链接", "原路径", "文件类型",
    "Raw准入结果", "解析结果", "语义画像结果", "关联结果", "页面结果",
    "发布结果", "回读结果", "下一步动作",
)


def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def materialize(name: str, replacements: dict[str, object]) -> str:
    text = (ASSETS / name).read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace("{{" + key + "}}", str(value))
    unresolved = re.findall(r"\{\{[A-Z0-9_]+\}\}", text)
    if unresolved:
        raise RuntimeError(f"交付模板占位符未替换：{unresolved}")
    return text.rstrip() + "\n"


def profile_map(job: Path) -> dict[str, dict]:
    result: dict[str, dict] = {}
    root = job / "02-extraction-cache" / "semantic" / "source-profiles"
    for path in root.glob("*.json"):
        value = load_json(path, {})
        if value.get("source_id"):
            result[str(value["source_id"])] = value
    return result


def state_by_stable_id(job: Path) -> dict[str, list[dict]]:
    state = load_json(job / "05-ledgers" / "钉钉发布状态.json", {})
    config = common.load_task_config(job)
    expected_root = str(config.get("publishing.publish_root_name", "publish_root_name", default="") or "")
    if expected_root and str((state.get("root") or {}).get("name") or "") != expected_root:
        return {}
    result: dict[str, list[dict]] = {}
    preview = job / "本地审核结果（仅供审核）"
    for original_record in (state.get("documents") or {}).values():
        record = dict(original_record)
        stable_id = str(record.get("stable_id") or "")
        if stable_id:
            current_page = preview / str(record.get("relative_path") or "")
            if current_page.is_file():
                current_sha256 = common.sha256_bytes(current_page.read_bytes())
                if current_sha256 != str(record.get("source_sha256") or ""):
                    record["update_status"] = "stale"
                    record["readback_status"] = "stale"
            result.setdefault(stable_id, []).append(record)
    return result


def aggregate_remote(records: list[dict], field: str, success: str, missing: str) -> str:
    if not records:
        return missing
    values = [str(record.get(field) or "未执行") for record in records]
    if any(value == "stale" for value in values):
        return "待更新（本地页面已变化）"
    if all(value == "success" for value in values) and len(records) >= 2:
        return success
    if any(value in {"failed", "blocked"} for value in values):
        return "失败：" + "、".join(sorted(set(values)))
    return "部分完成：" + "、".join(sorted(set(values)))


def parse_failure_action(source: dict) -> str:
    category = str(source.get("failure_category") or source.get("parse_status") or "")
    detail = str(source.get("last_error") or "")
    extension = str(source.get("extension") or source.get("content_type") or "文件")
    if category == "DWS未登录" or "未登录" in detail:
        return "重新登录DWS后，仅重试解析失败的钉钉表格"
    if category == "下载失败" or "媒体凭证" in detail:
        return f"检查父文档附件权限后，重新下载并解析内嵌{extension.upper()}文件"
    if "密码" in detail or "加密" in detail:
        return "由内容负责人提供未加密文件后重新解析"
    if "损坏" in detail:
        return "由内容负责人重新导出可正常打开的文件后再解析"
    return f"修复{category or '解析失败'}后重新解析"


def next_action(
    *, raw: str, parsed: str, semantic: str, pending_relation: bool,
    page: str, published: str, readback: str, error: str,
) -> str:
    if raw != "通过":
        return "按Raw入库标准重新提交可接受的文件，或保持排除"
    if parsed != "成功":
        return error or "修复权限、文件损坏或可检索性后重试"
    if semantic != "成功":
        return "重新执行Codex语义画像提取"
    if pending_relation:
        return "业务负责人确认高风险关联的有效口径"
    if page != "成功":
        return "重新生成两套完整知识页"
    if published == "未执行":
        return "完成本地审核后，在已配置最终目录时执行上线"
    if published.startswith("待更新"):
        return "将新版页面发布到已配置的钉钉目录并回读"
    if not published.startswith("成功"):
        return "重试发布失败页"
    if not readback.startswith("成功"):
        return "执行全量回读并修复不一致页"
    return "无必须动作；按需抽样检查"


def build(job: Path) -> dict:
    job = job.resolve()
    preview = job / "本地审核结果（仅供审核）"
    output = preview / "06-执行结果与下一步"
    output.mkdir(parents=True, exist_ok=True)
    config = common.load_task_config(job)
    kb_name = str(config.get("source.workspace_name", "workspace_name", default="部门知识库") or "部门知识库")
    manifest = common.load_manifest(job)
    profiles = profile_map(job)
    state = state_by_stable_id(job)
    ready = load_csv(job / "05-ledgers" / "relation-publication-ready.csv")
    pending = load_csv(job / "05-ledgers" / "relation-review-queue.csv")
    ready_ids = {str(row.get("source_id")) for row in ready} | {str(row.get("target_id")) for row in ready}
    pending_ids = {str(row.get("source_id")) for row in pending} | {str(row.get("target_id")) for row in pending}

    rows: list[dict] = []
    for source in manifest:
        source_id = str(source.get("source_id") or "")
        profile = profiles.get(source_id) or {}
        parse_ok = source.get("parse_status") in common.SUCCESS_STATUSES
        published = aggregate_remote(state.get(source_id, []), "update_status", "成功（两套视图）", "未执行")
        readback = aggregate_remote(state.get(source_id, []), "readback_status", "成功（两套视图）", "未执行")
        raw = "通过"
        parsed = "成功" if parse_ok else f"失败：{source.get('failure_category') or source.get('parse_status') or '未分类'}"
        semantic = "成功" if source_id in profiles else "未生成"
        relation = "已通过" if source_id in ready_ids else ("待负责人确认" if source_id in pending_ids else "未发现高信度关联")
        page = "成功" if source_id in profiles and all(
            any((preview / view).rglob(f"*（稳定ID：{source_id}）.md"))
            for view in ("01-原文镜像（按原目录）", "02-蒸馏结果（按文档类型）")
        ) else ("不适用" if not parse_ok else "未生成")
        error = parse_failure_action(source)
        action = next_action(
            raw=raw, parsed="成功" if parse_ok else "失败", semantic=semantic,
            pending_relation=source_id in pending_ids, page=page,
            published=published, readback=readback, error=error,
        )
        rows.append({
            "稳定ID": source_id,
            "文档标题": profile.get("file_name") or source.get("file_name") or source_id,
            "创建者": profile.get("creator_name") or "钉钉未返回（待补充）",
            "原文链接": common.sanitize_transient_url(
                profile.get("source_url") or source.get("source_url") or "",
                source.get("parent_node_id"),
            ),
            "原路径": source.get("source_path") or "",
            "文件类型": source.get("extension") or source.get("content_type") or "",
            "Raw准入结果": raw, "解析结果": parsed,
            "语义画像结果": semantic, "关联结果": relation, "页面结果": page,
            "发布结果": published, "回读结果": readback, "下一步动作": action,
        })

    raw_excluded_path = job / "01-inventory" / "raw-admission" / "经Raw标准排除清单.csv"
    scope_excluded_path = job / "01-inventory" / "excluded-scope" / "excluded-manifest.csv"
    raw_excluded_rows = load_csv(raw_excluded_path)
    scope_excluded_rows = load_csv(scope_excluded_path)
    excluded_paths = [raw_excluded_path, scope_excluded_path]
    existing_ids = {row["稳定ID"] for row in rows}
    for path in excluded_paths:
        for source in load_csv(path):
            source_id = str(source.get("source_id") or "")
            if not source_id or source_id in existing_ids:
                continue
            existing_ids.add(source_id)
            reason = source.get("admission_reason") or ("任务明确排除（不进入本次蒸馏）" if "excluded-scope" in path.as_posix() else "未说明")
            rows.append({
                "稳定ID": source_id, "文档标题": source.get("file_name") or source_id,
                "创建者": "未读取", "原文链接": common.sanitize_transient_url(
                    source.get("source_url") or "", source.get("parent_node_id")
                ),
                "原路径": source.get("source_path") or "", "文件类型": source.get("extension") or "",
                "Raw准入结果": f"排除：{reason}", "解析结果": "不执行", "语义画像结果": "不执行",
                "关联结果": "不执行", "页面结果": "不生成", "发布结果": "不执行", "回读结果": "不执行",
                "下一步动作": "保持排除；如需纳入，按Raw入库标准重新提交",
            })

    rows.sort(key=lambda row: (row["Raw准入结果"] != "通过", row["原路径"], row["稳定ID"]))
    matrix_path = output / "文档处理结果矩阵.csv"
    common.write_csv(matrix_path, rows, list(MATRIX_FIELDS))
    no_action_prefixes = ("无必须动作", "保持排除")
    actions = Counter(
        row["下一步动作"] for row in rows
        if not row["下一步动作"].startswith(no_action_prefixes)
    )
    action_path = output / "待办动作清单.md"
    action_lines = ["# 待办动作清单", "", "> 相同动作已合并；具体文档请在结果矩阵中筛选“下一步动作”。", ""]
    for index, (action, count) in enumerate(sorted(actions.items(), key=lambda item: (-item[1], item[0])), start=1):
        priority = "P0" if any(word in action for word in ("修复", "重试", "重新下载", "重新解析", "重新生成")) else ("P1" if any(word in action for word in ("确认", "上线", "回读")) else "P2")
        action_lines.append(f"- **{priority}** {action}：{count} 份")
    if not actions:
        action_lines.append("- 暂无需人工处理的事项。")
    action_lines.append("")
    action_path.write_text("\n".join(action_lines), encoding="utf-8")

    admitted = [row for row in rows if row["Raw准入结果"] == "通过"]
    success = [row for row in admitted if row["页面结果"] == "成功"]
    online = [row for row in admitted if row["回读结果"].startswith("成功")]
    mandatory = sum(not row["下一步动作"].startswith(no_action_prefixes) for row in rows)
    online_complete = bool(success) and len(online) == len(success)
    if online_complete and mandatory:
        status_line = f"- 当前状态：蒸馏后Wiki已上线并全量回读通过；仍有 {mandatory} 份记录待处理，任务未全部结束"
    elif online_complete:
        status_line = "- 当前状态：蒸馏后Wiki已上线并全量回读通过，当前无必须处理事项"
    else:
        status_line = "- 当前状态：尚未完成全量上线回读，任务未结束"
    result_rows = "\n".join([
        f"- 源空间盘点发现：{len(rows)} 份；按任务范围排除：{len(scope_excluded_rows)} 份",
        f"- 进入本次范围：{len(manifest) + len(raw_excluded_rows)} 份；Raw准入：{len(manifest)} 份；经Raw标准排除：{len(raw_excluded_rows)} 份",
        f"- 成功生成完整知识页：{len(success)} 份",
        f"- 解析失败：{sum(row['解析结果'] != '成功' for row in admitted)} 份",
        f"- 已通过关联：{len(ready)} 条；待负责人确认：{len(pending)} 条",
        f"- 已发布并回读通过：{len(online)} 份",
        status_line,
    ])
    next_actions = (
        f"- 必须处理的记录：{mandatory} 份；按优先级查看《待办动作清单》。\n"
        "- 高风险关联只由业务负责人判定；其他已通过关联只需抽样检查。\n"
        + ("- 上线验收已通过；后续重点是补齐解析失败文档和确认高风险关联。" if online_complete else "- 未通过全量回读前，不将任务标记为“已上线”。")
    )
    summary_path = preview / "00-蒸馏结果与下一步.md"
    summary_path.write_text(materialize("delivery-summary.md", {
        "KNOWLEDGE_BASE_NAME": kb_name, "RESULT_ROWS": result_rows, "NEXT_ACTIONS": next_actions,
        "MATRIX_LINK": quote("06-执行结果与下一步/文档处理结果矩阵.csv"),
        "ACTION_LIST_LINK": quote("06-执行结果与下一步/待办动作清单.md"),
        "REVIEW_ENTRY_LINK": quote("审核入口.md"),
    }), encoding="utf-8")
    return {"rows": len(rows), "successful_pages": len(success), "online": len(online), "mandatory_actions": mandatory}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a concise result matrix and next-action package.")
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.job)
    print("DELIVERY_SUMMARY_OK " + " ".join(f"{key}={value}" for key, value in result.items()))


if __name__ == "__main__":
    main()
