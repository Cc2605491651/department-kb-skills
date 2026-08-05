#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import re
import shutil

from config_utils import load_config, publication_target, validate_config_schema


SCRIPT_ROOT = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_ROOT.parent


def requirement(path: Path, label: str, errors: list[str]) -> None:
    if not path.exists():
        errors.append(f"缺少{label}：{path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate mandatory inputs and stage gates before running the pipeline.")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--target", default="")
    args = parser.parse_args()
    job = args.job.resolve()
    config = load_config(job)
    errors: list[str] = []
    warnings: list[str] = []

    if args.stage in {"inventory", "owners", "extract", "publish", "readback", "sync"} and not shutil.which("dws"):
        errors.append("未找到 dws 命令")
    if args.stage in {"semantic", "verify"} and not shutil.which("codex"):
        errors.append("未找到 codex 命令")

    requirement(config.path, "task-config.yaml", errors)
    requirement(job / "00-config" / "department-taxonomy.yaml", "部门业务分类配置", errors)
    requirement(job / "00-config" / "sensitive-scope.yaml", "敏感范围配置", errors)
    for path in (
        SKILL_ROOT / "assets" / "task-config.yaml",
        SKILL_ROOT / "assets" / "ai-query-entry.md",
        SKILL_ROOT / "assets" / "ai-knowledge-map.md",
        SKILL_ROOT / "assets" / "delivery-summary.md",
        SKILL_ROOT / "schemas" / "task-config.schema.json",
        SKILL_ROOT / "schemas" / "ai-document-header-v2.json",
        SKILL_ROOT / "references" / "raw-admission-standard.md",
        SKILL_ROOT / "references" / "01-全量蒸馏执行总规范.md",
        SKILL_ROOT / "references" / "02-双目录页面与17字段规范.md",
        SKILL_ROOT / "references" / "03-来源获取下载与解析执行规范.md",
        SKILL_ROOT / "references" / "format-compatibility-matrix.md",
        SKILL_ROOT / "references" / "04-Codex摘要与语义画像规范.md",
        SKILL_ROOT / "references" / "05-文档关系规则与审核分级规范.md",
        SKILL_ROOT / "references" / "06-业务确认发布回读与归档规范.md",
        SKILL_ROOT / "references" / "08-全流程验收清单.md",
        SKILL_ROOT / "references" / "10-名词解释与规则对照.md",
        SKILL_ROOT / "references" / "11-新知识库启动信息清单.md",
        SKILL_ROOT / "references" / "12-执行结果与下一步交付规范.md",
    ):
        requirement(path, "Skill必需资源", errors)

    errors.extend(validate_config_schema(config, SKILL_ROOT / "schemas" / "task-config.schema.json"))

    # 硬性门禁：必须显式声明 LLM 提供方，不允许静默默认
    llm_provider = str(config.get("llm.provider", "provider", default="") or "").strip()
    if not llm_provider:
        errors.append(
            "必须显式声明 LLM 提供方：在 task-config.yaml 配置 llm.provider"
            "（codex / siliconflow / deepseek=DeepSeek官方API / kimi），或运行时传 --llm-provider；不声明不执行"
        )
    elif llm_provider not in {"codex", "siliconflow", "deepseek", "kimi"}:
        errors.append(f"llm.provider 不支持的值：{llm_provider}（支持 codex / siliconflow / deepseek=DeepSeek官方API / kimi）")

    required_values = {
        "source.workspace_id": config.get("source.workspace_id", "workspace_id"),
        "source.workspace_url": config.get("source.workspace_url", "workspace_url"),
        "source.department": config.get("source.department", "department"),
        "source.stable_id_prefix": config.get("source.stable_id_prefix", "stable_id_prefix", "id_prefix"),
        "execution.execution_identity.executed_by": config.get("execution_identity.executed_by", "executed_by"),
    }
    for key, value in required_values.items():
        if not str(value or "").strip() or str(value).strip().startswith("<"):
            errors.append(f"配置未填写：{key}")
    for filename in ("task-config.yaml", "department-taxonomy.yaml", "sensitive-scope.yaml"):
        path = job / "00-config" / filename
        if path.exists() and re.search(r"<[A-Z][A-Z0-9_ -]*>", path.read_text(encoding="utf-8")):
            errors.append(f"配置仍有未替换占位符：{filename}")
    if config.get("execution_identity.owner_source", "owner_source") != "dingtalk_creator":
        errors.append("owner_source 必须为 dingtalk_creator")
    if int(config.get("content.required_metadata_fields", "required_metadata_fields", default=0) or 0) != 17:
        errors.append("required_metadata_fields 必须为17")
    for key in ("reject_standalone_images", "reject_audio", "reject_video", "reject_archives"):
        if not config.bool(f"raw_admission.{key}", key, default=False):
            errors.append(f"Raw准入必须启用：{key}")

    prerequisites = {
        "admission": [(job / "01-inventory" / "raw-manifest.json", "盘点清单")],
        "owners": [(job / "01-inventory" / "raw-manifest.json", "盘点清单")],
        "extract": [(job / "01-inventory" / "raw-admission" / "admission-summary.json", "Raw准入结果")],
        "semantic": [(job / "01-inventory" / "raw-manifest.json", "准入后清单")],
        "candidates": [(job / "05-ledgers" / "semantic-generation.csv", "语义画像台账")],
        "verify": [(job / "05-ledgers" / "relation-candidates.csv", "关系候选台账")],
        "review": [(job / "05-ledgers" / "relation-verification.csv", "关系核验台账")],
        "apply": [(job / "05-ledgers" / "relation-review-queue.csv", "关系审核清单")],
        "preview": [(job / "05-ledgers" / "relation-publication-ready.csv", "可发布关系台账")],
        "validate": [(job / "本地审核结果（仅供审核）", "本地审核结果")],
        "confirm": [
            (job / "06-reports" / "local-acceptance.json", "本地验收结果"),
            (job / "02-extraction-cache" / "semantic" / "source-profiles", "成功文档来源画像"),
        ],
        "publish": [(job / "06-reports" / "local-acceptance.json", "本地验收结果")],
        "readback": [(job / "05-ledgers" / "钉钉发布状态.json", "发布状态台账")],
    }
    for path, label in prerequisites.get(args.stage, []):
        requirement(path, label, errors)

    if args.stage in {"publish", "readback"}:
        configured_target = publication_target(config)
        requested_target = args.target.strip() or configured_target
        if not configured_target:
            errors.append("任务启动配置未启用 publishing 或未提供 target_folder_url")
        elif requested_target and re.sub(r"[?#].*$", "", requested_target) != re.sub(r"[?#].*$", "", configured_target):
            errors.append("本次发布目标与任务启动时配置的 target_folder_url 不一致")
        acceptance_path = job / "06-reports" / "local-acceptance.json"
        if acceptance_path.exists():
            try:
                if json.loads(acceptance_path.read_text(encoding="utf-8")).get("passed") is not True:
                    errors.append("本地验收未通过")
            except Exception:
                errors.append("local-acceptance.json 无法解析")
    if args.stage == "publish":
        queue_path = job / "05-ledgers" / "relation-review-queue.csv"
        if queue_path.exists():
            with queue_path.open(encoding="utf-8", newline="") as handle:
                pending_l3 = [
                    row.get("relation_id", "")
                    for row in csv.DictReader(handle)
                    if row.get("review_status") in {"待确认", "待复审", "暂缓", ""}
                ]
            if pending_l3:
                warnings.append(f"仍有{len(pending_l3)}条L3关系未处置；这些关系必须从正式页面和relations中排除，不阻断其他已验收内容上线")

    report = {
        "passed": not errors,
        "stage": args.stage,
        "job": str(job),
        "configured_publish_target": publication_target(config),
        "errors": errors,
        "warnings": warnings,
    }
    report_path = job / "06-reports" / "preflight.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if errors:
        raise SystemExit("PREFLIGHT_FAILED\n- " + "\n- ".join(errors))
    print(f"PREFLIGHT_OK stage={args.stage}")


if __name__ == "__main__":
    main()
