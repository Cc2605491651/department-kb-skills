#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


ASSETS = Path(__file__).resolve().parents[1] / "assets"


def materialize(source: Path, target: Path, replacements: dict[str, str], force: bool) -> None:
    if target.exists() and not force:
        raise SystemExit(f"配置已存在，未覆盖：{target}")
    text = source.read_text(encoding="utf-8")
    for old, new in replacements.items():
        text = text.replace(old, new)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a clean knowledge-base distillation job from bundled templates.")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--department", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--workspace-url", required=True)
    parser.add_argument("--workspace-name", required=True)
    parser.add_argument("--id-prefix", required=True)
    parser.add_argument("--executed-by", required=True)
    parser.add_argument("--publish-target", default="")
    parser.add_argument("--publish-root-name", default="")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    job = args.job.resolve()
    config = job / "00-config"
    for directory in (
        config,
        job / "01-inventory",
        job / "02-extraction-cache",
        job / "03-local-output",
        job / "04-review",
        job / "05-ledgers",
        job / "06-reports",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    target = args.publish_target.strip()
    publish_root = args.publish_root_name.strip() or f"{args.workspace_name}知识库蒸馏结果"
    replacements = {
        "<TASK_ID>": args.task_id,
        "<SOURCE_WORKSPACE_NAME>": args.workspace_name,
        "<SOURCE_WORKSPACE_ID>": args.workspace_id,
        "<SOURCE_WORKSPACE_URL>": args.workspace_url,
        "<DEPARTMENT_NAME>": args.department,
        "<DEPARTMENT_PREFIX>": args.id_prefix.upper(),
        "<YYYY-MM-DD HH:mm:ss +0800>": "待执行时自动记录",
        "<INCLUDE_PATH>": "/",
        "<ABSOLUTE_LOCAL_WORKBENCH_PATH>": str(job),
        "<ABSOLUTE_DELIVERY_PATH>": str(job / "03-local-output"),
        "<AI_TASK_INITIATOR>": args.executed_by,
        "<TARGET_FOLDER_URL_IF_AUTHORIZED>": target,
        "<NEW_ROOT_FOLDER_NAME>": publish_root,
    }
    materialize(ASSETS / "task-config.yaml", config / "task-config.yaml", replacements, args.force)
    materialize(ASSETS / "department-taxonomy.yaml", config / "department-taxonomy.yaml", {"<DEPARTMENT_NAME>": args.department}, args.force)
    materialize(ASSETS / "sensitive-scope.yaml", config / "sensitive-scope.yaml", {"<DEPARTMENT_NAME>": args.department}, args.force)
    materialize(
        ASSETS / "AGENTS.md",
        job / "AGENTS.md",
        {
            "<KNOWLEDGE_BASE_NAME>": args.workspace_name,
            "<SOURCE_WORKSPACE_ID>": args.workspace_id,
            "<DISTILLED_WORKSPACE_ID>": "发布后由回读台账补充" if target else "仅本地，不适用",
            "<LOCAL_MACHINE_INDEX_PATH>": str(job / "01-inventory"),
            "<RELATION_REGISTRY_PATH>": str(job / "05-ledgers" / "relation-ledger.csv"),
        },
        args.force,
    )

    config_text = (config / "task-config.yaml").read_text(encoding="utf-8")
    config_text = config_text.replace("publishing:\n  enabled: false", f"publishing:\n  enabled: {'true' if target else 'false'}")
    if target:
        config_text = config_text.replace('  local_only: true', '  local_only: false')
        config_text = config_text.replace('  remote_write_policy: "forbidden"', '  remote_write_policy: "publish_target_only"')
        config_text = config_text.replace('  remote_publish_policy: "forbidden"', '  remote_publish_policy: "configured_target_only"')
    (config / "task-config.yaml").write_text(config_text, encoding="utf-8")
    print(f"JOB_BOOTSTRAP_OK job={job} publish={'enabled' if target else 'disabled'}")


if __name__ == "__main__":
    main()
