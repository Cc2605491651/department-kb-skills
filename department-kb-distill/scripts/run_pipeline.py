#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

from config_utils import is_local_only, load_config, publication_target


SCRIPTS = Path(__file__).resolve().parent


def run(script: str, args: list[str]) -> None:
    command = [sys.executable, str(SCRIPTS / script), *args]
    print("RUN", script, flush=True)
    result = subprocess.run(command)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the department knowledge-base distillation pipeline.")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--stage", choices=["preflight", "inventory", "owners", "admission", "extract", "semantic", "candidates", "verify", "review", "sync", "apply", "preview", "validate", "confirm", "publish", "readback", "all"], required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--model", default="")
    parser.add_argument("--llm-provider", default="", help="codex|siliconflow|kimi，默认 codex（codex 订阅额度）")
    parser.add_argument("--llm-model", default="", help="第三方实际模型名；为空用 provider 默认模型")
    parser.add_argument("--llm-api-key-env", default="", help="第三方 API key 所在环境变量名（key 只经环境变量传入，不落盘）")
    parser.add_argument("--no-ai-cache", action="store_true", help="跳过 AI 画像/核验缓存，每次真实调用（第三方默认强制开启）")
    parser.add_argument("--create-only", action="store_true")
    parser.add_argument("--target", default="")
    parser.add_argument("--root-name", default="")
    parser.add_argument("--publish-workers", type=int, default=30)
    parser.add_argument("--smoke-groups", type=int, default=0)
    parser.add_argument("--retry-failed-only", action="store_true")
    parser.add_argument("--last-scope", action="store_true")
    parser.add_argument("--confirmation-text", default="")
    parser.add_argument("--confirmed-by", default="")
    parser.add_argument("--source-id", action="append", default=[], help="只处理指定稳定ID；可重复传入")
    args = parser.parse_args()
    config = load_config(args.job)
    local_only = is_local_only(config)
    configured_target = publication_target(config)
    target = args.target.strip() or configured_target
    root_name = args.root_name.strip() or str(config.get("publishing.publish_root_name", "publish_root_name", default="") or "")
    base = ["--job", str(args.job.resolve())]
    semantic = [*base, "--workers", str(args.workers)]
    verify = [*base, "--workers", str(args.workers)]
    selected_sources = [item for source_id in args.source_id for item in ("--source-id", source_id)]
    semantic.extend(selected_sources)
    if args.limit:
        semantic.extend(["--limit", str(args.limit)])
        verify.extend(["--limit", str(args.limit)])
    # LLM 提供方：命令行优先，否则读任务配置（配置缺失由 preflight 硬性拦截）
    llm_provider = args.llm_provider or str(config.get("llm.provider", "provider", default="") or "")
    if not llm_provider:
        raise SystemExit("必须显式声明 LLM 提供方：配置 task-config.yaml 的 llm.provider 或传 --llm-provider")
    if llm_provider != "codex" and args.stage in {"semantic", "verify", "all"}:
        # 第三方：key 必须已注入环境变量；API key 绝不写入命令行、job 目录或台账
        # 仅语义/核验阶段需要 key；preflight 等本地阶段不要求
        if not args.llm_api_key_env:
            raise SystemExit("第三方 LLM 提供方必须提供 --llm-api-key-env（API key 所在环境变量名）")
        if not os.environ.get(args.llm_api_key_env, ""):
            raise SystemExit(f"环境变量 {args.llm_api_key_env} 未设置；请先 export 该变量再执行")
    if llm_provider != "codex":
        llm_extra = ["--llm-provider", llm_provider]
        if args.llm_model:
            llm_extra.extend(["--llm-model", args.llm_model])
        llm_extra.extend(["--llm-api-key-env", args.llm_api_key_env, "--no-ai-cache"])
        semantic.extend(llm_extra)
        verify.extend(llm_extra)
    elif args.no_ai_cache:
        semantic.append("--no-ai-cache")
        verify.append("--no-ai-cache")
    if args.model:
        semantic.extend(["--model", args.model])
        verify.extend(["--model", args.model])
    if args.stage == "all":
        stages = (["owners"] if (args.job / "01-inventory" / "raw-manifest.json").exists() else ["inventory"]) + ["admission", "extract", "semantic", "candidates", "verify", "review", "apply", "preview", "validate"]
    else:
        stages = [args.stage]
    for stage in stages:
        preflight_args = [*base, "--stage", stage]
        if stage in {"publish", "readback"} and target:
            preflight_args.extend(["--target", target])
        if stage != "preflight":
            run("preflight.py", preflight_args)
        if stage == "preflight":
            run("preflight.py", [*base, "--stage", "inventory"])
        elif stage == "inventory":
            run("inventory_wiki.py", base)
            run("enrich_creators.py", [*base, "--workers", str(args.workers)])
        elif stage == "owners":
            run("enrich_creators.py", [*base, "--workers", str(args.workers)])
        elif stage == "admission":
            run("apply_raw_admission.py", base)
        elif stage == "extract":
            run("extract_sources.py", [*base, "--workers", str(args.workers), *(["--limit", str(args.limit)] if args.limit else []), *selected_sources])
        elif stage == "semantic":
            run("codex_semantic.py", semantic)
        elif stage == "candidates":
            run("build_relation_candidates.py", base)
        elif stage == "verify":
            run("codex_verify_relations.py", verify)
        elif stage == "review":
            run("build_review_queue.py", base)
        elif stage == "sync":
            if local_only:
                raise SystemExit("LOCAL_ONLY_GUARD: 当前任务禁止线上同步")
            run("sync_review_aitable.py", [*base, *(["--create-only"] if args.create_only else [])])
        elif stage == "apply":
            run("apply_review_decisions.py", base)
        elif stage == "preview":
            run("render_review_package.py", base)
        elif stage == "validate":
            run("validate_distillation.py", base)
        elif stage == "confirm":
            if not args.confirmation_text.strip():
                raise SystemExit("confirm必须传入执行者在AI对话框中的明确确认原话：--confirmation-text")
            confirm_args = [*base, "--confirmation-text", args.confirmation_text]
            if args.confirmed_by.strip():
                confirm_args.extend(["--confirmed-by", args.confirmed_by])
            run("confirm_distillation.py", confirm_args)
            run("render_review_package.py", base)
            run("validate_distillation.py", base)
        elif stage == "publish":
            if not target or not root_name:
                raise SystemExit("publish必须在task-config.yaml提供target_folder_url和publish_root_name（命令行参数仅可重复同一目标）")
            publish_args = [
                *base,
                "--target", target,
                "--root-name", root_name,
                "--workers", str(max(1, min(args.publish_workers, 30))),
            ]
            if args.smoke_groups:
                publish_args.extend(["--smoke-groups", str(args.smoke_groups)])
            if args.retry_failed_only:
                publish_args.append("--retry-failed-only")
            run("publish_wiki.py", publish_args)
        elif stage == "readback":
            if not target:
                raise SystemExit("readback必须在task-config.yaml提供target_folder_url")
            readback_args = [
                *base,
                "--target", target,
                "--workers", str(max(1, min(args.publish_workers, 30))),
            ]
            if args.last_scope:
                readback_args.append("--last-scope")
            if args.retry_failed_only:
                readback_args.append("--retry-failed-only")
            run("readback_wiki.py", readback_args)


if __name__ == "__main__":
    main()
