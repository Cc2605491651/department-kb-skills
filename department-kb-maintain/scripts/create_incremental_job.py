#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil

import maintain_common as common


COPY_DIRECTORIES = (
    "00-config",
    "01-inventory",
    "02-extraction-cache",
    "05-ledgers",
    "本地审核结果（仅供审核）",
)
SKIP_NAMES = {".DS_Store", ".maintenance.lock", "钉钉发布授权.json", "incremental"}


def ignored(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in SKIP_NAMES or name == "__pycache__" or name.endswith(".pyc")}


def yaml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def replace_yaml_value(text: str, key: str, value: str, *, section: str | None = None) -> str:
    lines = text.splitlines()
    in_section = section is None
    section_indent = 0
    replaced = False
    for index, line in enumerate(lines):
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if section is not None and indent == 0 and stripped == f"{section}:":
            in_section = True
            section_indent = indent
            continue
        if section is not None and in_section and stripped and indent <= section_indent:
            in_section = False
        if in_section and re.match(rf"^{re.escape(key)}\s*:", stripped):
            prefix = line[:indent]
            lines[index] = f"{prefix}{key}: {yaml_quote(value)}"
            replaced = True
            break
    if not replaced:
        raise RuntimeError(f"task-config.yaml缺少字段：{section + '.' if section else ''}{key}")
    return "\n".join(lines) + "\n"


def copy_job(baseline_job: Path, job: Path, *, task_id: str = "") -> dict:
    baseline = baseline_job.expanduser().resolve()
    target = job.expanduser().resolve()
    if baseline == target:
        raise RuntimeError("增量任务目录必须与全量基线目录分开")
    if target.is_relative_to(baseline) or baseline.is_relative_to(target):
        raise RuntimeError("增量任务目录与全量基线目录不能互相嵌套")
    required = (
        baseline / "00-config" / "task-config.yaml",
        baseline / "01-inventory" / "raw-manifest.json",
        baseline / "06-reports" / "local-acceptance.json",
        baseline / "本地审核结果（仅供审核）",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError("全量基线不完整，缺少：" + "、".join(missing))
    acceptance = common.load_json(baseline / "06-reports" / "local-acceptance.json", {}) or {}
    if acceptance.get("passed") is not True:
        raise RuntimeError("全量基线的本地验收未通过，不能创建增量任务")
    if target.exists() and any(target.iterdir()):
        raise RuntimeError(f"目标目录不是空目录：{target}")
    target.mkdir(parents=True, exist_ok=True)
    for name in COPY_DIRECTORIES:
        source = baseline / name
        if source.exists():
            shutil.copytree(source, target / name, dirs_exist_ok=True, ignore=ignored)
    reports = target / "06-reports"
    reports.mkdir(parents=True, exist_ok=True)
    shutil.copy2(baseline / "06-reports" / "local-acceptance.json", reports / "local-acceptance.json")
    maintenance_template = Path(__file__).resolve().parents[1] / "assets" / "maintenance-config.yaml"
    shutil.copy2(maintenance_template, target / "00-config" / "maintenance-config.yaml")

    old_task_id = str(common.task_value(baseline, "task_id", "") or "")
    new_task_id = task_id.strip() or f"{old_task_id or 'KB'}-INC-{common.run_id()}"
    config_path = target / "00-config" / "task-config.yaml"
    config_text = config_path.read_text(encoding="utf-8")
    config_text = replace_yaml_value(config_text, "task_id", new_task_id)
    config_text = replace_yaml_value(config_text, "local_workbench", str(target), section="execution")
    config_text = replace_yaml_value(config_text, "delivery_directory", str(target), section="execution")
    config_path.write_text(config_text, encoding="utf-8")

    # Keep the accepted full manifest as the initial successful comparison state.
    # Previous incremental state is intentionally discarded so this job starts
    # with a clearly auditable baseline copied from the selected source folder.
    source_manifest = common.load_json(baseline / "01-inventory" / "raw-manifest.json", []) or []
    reference = {
        "schema_version": "incremental-baseline-reference-v1",
        "created_at": common.now_iso(),
        "baseline_job": str(baseline),
        "baseline_task_id": old_task_id,
        "incremental_task_id": new_task_id,
        "baseline_manifest_hash": common.canonical_hash(source_manifest),
        "baseline_document_count": len(source_manifest),
        "source_workspace_url": str(common.task_value(baseline, "source.workspace_url", "") or ""),
        "publishing_target_preserved": bool(str(common.task_value(target, "publishing.target_folder_url", "") or "").strip()),
    }
    common.write_json(target / "00-config" / "baseline-reference.json", reference)
    instructions = f"""# 增量维护任务说明

- 原全量结果：`{baseline}`
- 本次维护结果保存到：`{target}`
- 已带入文档：{len(source_manifest)}份
- 创建时间：{reference['created_at']}
- 当前情况：准备工作已经完成，尚未开始检查或处理文档，也没有上传到钉钉。

下一步：让AI开始增量维护。完成后先查看《00-增量结果与下一步》和《01-本次增量审核入口》，明确确认后再决定是否上线。
"""
    common.atomic_write(target / "00-本次增量任务说明.md", instructions)
    print(f"INCREMENTAL_JOB_OK job={target} baseline_documents={len(source_manifest)}")
    return reference


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an independent incremental job from an accepted full-distillation baseline.")
    parser.add_argument("--baseline-job", type=Path, required=True)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--task-id", default="")
    args = parser.parse_args()
    copy_job(args.baseline_job, args.job, task_id=args.task_id)


if __name__ == "__main__":
    main()
