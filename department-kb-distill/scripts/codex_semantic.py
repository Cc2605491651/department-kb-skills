#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_common as common
import llm_providers


DIRECT_CHARS = 55_000
REQUEST_CHARS = 120_000
MAX_ITEMS = 1
SCHEMA_NAME = "semantic-output-v1.json"
BUNDLED_SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / SCHEMA_NAME
LEDGER_FIELDS = [
    "source_id", "content_hash", "content_profile_path", "source_profile_path",
    "summary", "page_type_candidate", "document_role", "prompt_version",
    "schema_hash", "codex_cli_version", "model", "generated_at", "status",
]
TASK_LOG_LOCK = threading.Lock()


def clean_list(value: Any, limit: int = 30) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = re.sub(r"\s+", " ", str(item)).strip()
        if text and text not in result:
            result.append(text[:500])
        if len(result) >= limit:
            break
    return result


def validate_item(item: dict, expected_key: str) -> dict:
    if item.get("document_key") != expected_key:
        raise RuntimeError(f"Codex 返回 document_key 不匹配：{item.get('document_key')} != {expected_key}")
    summary = re.sub(r"\s+", " ", str(item.get("summary") or "")).strip()
    if not summary:
        raise RuntimeError(f"{expected_key} 的摘要为空")
    if re.match(r"^(?:老大|老板|领导|用户|您好)[，,：:\s]", summary):
        raise RuntimeError(f"{expected_key} 的摘要包含对话称呼")
    serialized = json.dumps(item, ensure_ascii=False)
    if re.search(r"(?i)(?:密码|password|passcode)\s*[:：=]\s*(?!\[REDACTED\])[^\s,，;；。]{3,}", serialized):
        raise RuntimeError(f"{expected_key} 的画像复述了敏感凭据")
    for key in [
        "scenarios", "keywords", "business_objects", "entities", "inputs", "actions", "outputs",
        "version_signals", "time_signals", "constraints", "relation_clues", "evidence_locations", "warnings",
    ]:
        item[key] = clean_list(item.get(key))
    references: list[dict] = []
    for reference in item.get("explicit_references") or []:
        if not isinstance(reference, dict):
            continue
        target = re.sub(r"\s+", " ", str(reference.get("target_text") or "")).strip()
        evidence = re.sub(r"\s+", " ", str(reference.get("evidence") or "")).strip()
        if target:
            references.append({"target_text": target[:500], "evidence": evidence[:500]})
    item["explicit_references"] = references[:30]
    item["summary"] = summary[:1200]
    item["page_type_candidate"] = str(item.get("page_type_candidate") or "未识别")[:100]
    item["core_theme"] = str(item.get("core_theme") or "未识别")[:500]
    item["document_role"] = str(item.get("document_role") or "未识别")[:100]
    return item


def semantic_prompt(tasks: list[dict], *, rollup: bool = False) -> str:
    mode = "片段画像汇总" if rollup else "原文语义提取"
    payload = [
        {
            "document_key": task["document_key"],
            "title": task.get("title", ""),
            "source_path": task.get("source_path", ""),
            "file_type": task.get("extension", ""),
            "content_mode": task.get("content_mode", "full_text"),
            "content": task["content"],
        }
        for task in tasks
    ]
    return f"""你正在执行部门知识库的{mode}。请对输入数组逐项返回结构化画像，并严格保持 document_key。

规则：
1. 只能写输入中明确存在的事实；无证据时使用空数组或“未识别”，禁止从标题、目录、常识推测正文事实。
2. summary 直接说明业务对象、目的、关键内容和适用场景，不使用“本文主要介绍”“该文档讲述”等套话。
3. page_type_candidate 使用简短类型，例如制度、流程、模板、案例、项目、会议纪要、方案、数据、培训、总览、记录、附件。
4. document_role 描述它在业务链路中的角色，例如规范、输入、执行流程、交付物、模板、证据、案例。
5. inputs/actions/outputs 只记录原文明示内容；explicit_references 保存被引用目标的原文名称/ID/链接及证据位置。
6. evidence_locations 使用页码标记、幻灯片标记、章节标题或可复核短语定位，不复制长段原文。
7. 独立图片、音频、视频、压缩包和其他Raw拒绝格式不得出现在输入中；发现时必须停止该项处理并报告准入错误。
8. 若输入是片段画像汇总，合并全部片段事实、去重并指出片段间冲突；不得新增片段中没有的信息。
9. 这是正式数据产物，不得出现“老大、老板、用户、您好”等对话称呼，不得向读者致意。
10. 不得输出密码、口令、Token、API Key、私钥等敏感值；输入中的 [REDACTED] 必须保持脱敏，只能在 warnings 说明“原文含敏感凭据”。

输入 JSON：
{json.dumps(payload, ensure_ascii=False)}
"""


def split_text(text: str, limit: int = DIRECT_CHARS) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        hard_end = min(len(text), start + limit)
        end = hard_end
        if hard_end < len(text):
            for marker in ("\n#", "\n[[PAGE ", "\n[[ppt/slides/slide", "\n\n", "\n"):
                boundary = text.rfind(marker, start + limit // 2, hard_end)
                if boundary > start:
                    end = boundary + 1
                    break
        chunks.append(text[start:end])
        start = end
    if "".join(chunks) != text:
        raise RuntimeError("长文切片未能逐字还原")
    return chunks


def pack_tasks(tasks: list[dict], max_chars: int, max_items: int) -> list[list[dict]]:
    batches: list[list[dict]] = []
    current: list[dict] = []
    chars = 0
    for task in tasks:
        task_chars = len(task["content"])
        if current and (len(current) >= max_items or chars + task_chars > max_chars):
            batches.append(current)
            current, chars = [], 0
        current.append(task)
        chars += task_chars
    if current:
        batches.append(current)
    return batches


def content_profile_path(paths: dict[str, Path], content_hash: str) -> Path:
    return paths["content_profiles"] / f"{content_hash}.json"


def valid_profile(path: Path, content_hash: str, schema_hash: str, provider: str | None = "") -> dict | None:
    """校验画像缓存。provider=None 表示不检查提供方（仅用于汇总已生成画像）；否则必须精确匹配。"""
    payload = common.load_json(path)
    if not isinstance(payload, dict):
        return None
    meta = payload.get("_meta") or {}
    if meta.get("input_hash") != content_hash or meta.get("prompt_version") != common.PROMPT_VERSION or meta.get("schema_hash") != schema_hash:
        return None
    if provider is not None and meta.get("provider", "") != provider:
        return None
    try:
        return validate_item(payload, content_hash)
    except Exception:
        return None


def log_task(paths: dict[str, Path], record: dict) -> None:
    common.append_jsonl(paths["ledgers"] / "codex-task-log.jsonl", record)


def run_batch(
    batch: list[dict], *, paths: dict[str, Path], schema_path: Path, schema_hash: str,
    model: str, provider: str = "", api_key_env: str = "", rollup: bool = False,
) -> dict[str, dict]:
    task_kind = "semantic_rollup" if rollup else "semantic_extract"
    keys = [task["document_key"] for task in batch]
    prompt = semantic_prompt(batch, rollup=rollup)
    task_id = hashlib.sha256((task_kind + common.PROMPT_VERSION + "|".join(keys) + common.sha256_text(prompt)).encode()).hexdigest()[:20]
    base_log = {
        "task_id": task_id,
        "task_kind": task_kind,
        "document_keys": keys,
        "input_hash": common.sha256_text(prompt),
        "prompt_version": common.PROMPT_VERSION,
        "prompt_hash": common.sha256_text(semantic_prompt([{**batch[0], "content": "<CONTENT>"}], rollup=rollup)),
        "schema_hash": schema_hash,
        "input_chars": len(prompt),
    }
    try:
        payload, metadata = llm_providers.run_llm_structured(
            prompt=prompt, schema_path=schema_path, cwd=paths["job"],
            provider=provider, model=model, api_key_env=api_key_env,
        )
        items = payload.get("items") or []
        if len(items) != len(batch):
            raise RuntimeError(f"Codex 返回 {len(items)} 项，预期 {len(batch)} 项")
        by_key = {str(item.get("document_key")): item for item in items if isinstance(item, dict)}
        if set(by_key) != set(keys):
            raise RuntimeError("Codex 返回的 document_key 集合与输入不一致")
        result = {key: validate_item(by_key[key], key) for key in keys}
        for task in batch:
            item = result[task["document_key"]]
            if task.get("chunk_path"):
                chunk_item = {**item, "_chunk_input_hash": common.sha256_text(task["content"])}
                common.write_json(Path(task["chunk_path"]), chunk_item)
            elif not rollup and ":chunk:" not in task["document_key"]:
                redactions = task.get("redaction_warnings") or []
                if redactions:
                    notice = "输入在送入 Codex 前已脱敏：" + "、".join(redactions)
                    if notice not in item["warnings"]:
                        item["warnings"].append(notice)
                item["_meta"] = {
                    "input_hash": task["document_key"], "prompt_version": common.PROMPT_VERSION,
                    "prompt_hash": base_log["prompt_hash"], "schema_hash": schema_hash,
                    "codex_cli_version": metadata["codex_cli_version"], "model": metadata["model"],
                    "provider": metadata.get("provider", provider),
                    "generated_at": metadata["finished_at"], "content_mode": task.get("content_mode", "full_text"),
                    "input_chars": len(task["content"]), "chunks": 1, "attempts": metadata["attempts"],
                }
                common.write_json(content_profile_path(paths, task["document_key"]), item)
        log_task(paths, {**base_log, **metadata, "status": "success", "output_chars": len(json.dumps(payload, ensure_ascii=False)), "error": ""})
        return result
    except Exception as error:
        log_task(paths, {**base_log, "finished_at": common.now_iso(), "status": "failed", "output_chars": 0, "error": common.safe_error(error), "model": model or "default"})
        if len(batch) > 1:
            middle = len(batch) // 2
            return {
                **run_batch(batch[:middle], paths=paths, schema_path=schema_path, schema_hash=schema_hash, model=model, provider=provider, api_key_env=api_key_env, rollup=rollup),
                **run_batch(batch[middle:], paths=paths, schema_path=schema_path, schema_hash=schema_hash, model=model, provider=provider, api_key_env=api_key_env, rollup=rollup),
            }
        raise


def process_batches(
    batches: list[list[dict]], *, workers: int, paths: dict[str, Path], schema_path: Path,
    schema_hash: str, model: str, provider: str = "", api_key_env: str = "", rollup: bool = False,
) -> dict[str, dict]:
    results: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [
            pool.submit(run_batch, batch, paths=paths, schema_path=schema_path, schema_hash=schema_hash, model=model, provider=provider, api_key_env=api_key_env, rollup=rollup)
            for batch in batches
        ]
        for future in concurrent.futures.as_completed(futures):
            try:
                results.update(future.result())
            except RuntimeError as error:
                print(f"SEMANTIC_BATCH_FAILED {common.safe_error(error)}", flush=True)
            print(f"SEMANTIC_PROGRESS completed={len(results)}", flush=True)
    return results


def build_source_profiles(rows: list[dict], paths: dict[str, Path], schema_hash: str) -> list[dict]:
    config = common.load_task_config(paths["job"])
    workspace_id = str(config.get("source.workspace_id", "workspace_id", default="") or "")
    ledger: list[dict] = []
    summary_rows: list[dict] = []
    for row in rows:
        if row.get("parse_status") not in common.SUCCESS_STATUSES:
            continue
        content_hash = str(row.get("extracted_hash") or row.get("source_hash") or "")
        if not content_hash:
            continue
        profile_path = content_profile_path(paths, content_hash)
        profile = valid_profile(profile_path, content_hash, schema_hash, provider=None)
        if not profile:
            continue
        source_profile = {
            "source_id": row.get("source_id", ""),
            "department": row.get("department", ""),
            "workspace_id": workspace_id,
            "node_id": row.get("node_id", ""),
            "source_url": row.get("source_url", ""),
            "source_path": row.get("source_path", ""),
            "file_name": row.get("file_name", ""),
            "extension": row.get("extension", ""),
            "create_time": row.get("create_time", ""),
            "update_time": row.get("update_time", ""),
            "creator_uid": row.get("creator_uid", ""),
            "creator_name": row.get("creator_name", ""),
            "owner": row.get("creator_name") or row.get("owner") or "待补充",
            "permission_snapshot": row.get("permission_snapshot", ""),
            "source_hash": row.get("source_hash", ""),
            "content_profile_hash": content_hash,
            "semantic_profile_path": str(profile_path.relative_to(paths["job"])),
            "content_profile": {key: value for key, value in profile.items() if key != "_meta"},
            "generated_at": common.now_iso(),
        }
        source_path = paths["source_profiles"] / f"{row['source_id']}.json"
        common.write_json(source_path, source_profile)
        meta = profile.get("_meta") or {}
        ledger.append({
            "source_id": row["source_id"],
            "content_hash": content_hash,
            "content_profile_path": str(profile_path.relative_to(paths["job"])),
            "source_profile_path": str(source_path.relative_to(paths["job"])),
            "summary": profile.get("summary", ""),
            "page_type_candidate": profile.get("page_type_candidate", ""),
            "document_role": profile.get("document_role", ""),
            "prompt_version": meta.get("prompt_version", ""),
            "schema_hash": meta.get("schema_hash", ""),
            "codex_cli_version": meta.get("codex_cli_version", ""),
            "model": meta.get("model", "default"),
            "generated_at": meta.get("generated_at", ""),
            "status": "success",
        })
        summary_rows.append({
            "source_id": row["source_id"], "parse_status": row.get("parse_status", ""),
            "model": f"codex:{meta.get('model', 'default')}", "prompt_hash": meta.get("prompt_hash", ""),
            "input_hash": content_hash, "content_mode": meta.get("content_mode", ""),
            "input_chars": meta.get("input_chars", ""), "chunks": meta.get("chunks", ""),
            "attempts": meta.get("attempts", ""), "summary_chars": len(str(profile.get("summary", ""))),
            "length_status": "structured_profile", "summary": profile.get("summary", ""),
            "generated_at": meta.get("generated_at", ""), "result": "codex_profile",
        })
    common.write_csv(paths["ledgers"] / "semantic-generation.csv", ledger, LEDGER_FIELDS)
    common.write_csv(paths["ledgers"] / "summary-generation.csv", summary_rows, [
        "source_id", "parse_status", "model", "prompt_hash", "input_hash", "content_mode",
        "input_chars", "chunks", "attempts", "summary_chars", "length_status", "summary", "generated_at", "result",
    ])
    return ledger


def main() -> None:
    parser = argparse.ArgumentParser(description="Use Codex or third-party LLM to generate per-document semantic profiles.")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--model", default="")
    parser.add_argument("--source-id", action="append", default=[])
    parser.add_argument("--llm-provider", default="", help="codex|siliconflow|kimi，默认 codex")
    parser.add_argument("--llm-model", default="", help="第三方实际模型名；为空用 provider 默认模型")
    parser.add_argument("--llm-api-key-env", default="", help="第三方 API key 所在环境变量名")
    parser.add_argument("--no-ai-cache", action="store_true", help="跳过 AI 画像缓存，每次真实调用")
    args = parser.parse_args()

    # resolve 返回第三项是 key 值，只做校验不沿用；后续一律用 args.llm_api_key_env（环境变量名）传递
    llm_provider = args.llm_provider or str(common.load_task_config(args.job).get("llm.provider", "provider", default="") or "")
    if not llm_provider:
        raise RuntimeError("必须显式声明 LLM 提供方：配置 task-config.yaml 的 llm.provider 或传 --llm-provider")
    provider, model, _ = llm_providers.resolve(llm_provider, args.llm_model or args.model, args.llm_api_key_env)
    if not llm_providers.is_codex(provider) and not os.environ.get(args.llm_api_key_env or llm_providers.default_key_env(provider), ""):
        raise RuntimeError(f"第三方 LLM 缺少 API key：请先设置环境变量 {args.llm_api_key_env or llm_providers.default_key_env(provider)}")
    api_key_env = args.llm_api_key_env

    paths = common.job_paths(args.job)
    for key in ("content_profiles", "source_profiles", "chunk_profiles", "schemas", "ledgers", "reports"):
        paths[key].mkdir(parents=True, exist_ok=True)
    schema_path = paths["schemas"] / SCHEMA_NAME
    if not BUNDLED_SCHEMA.exists():
        raise RuntimeError(f"Skill缺少语义输出Schema：{BUNDLED_SCHEMA}")
    common.atomic_write(schema_path, BUNDLED_SCHEMA.read_bytes())
    schema_hash = common.sha256_bytes(schema_path.read_bytes())
    rows = common.load_manifest(args.job)
    if args.source_id:
        selected = set(args.source_id)
        rows_for_generation = [row for row in rows if row.get("source_id") in selected]
    else:
        rows_for_generation = rows

    by_hash: dict[str, dict] = {}
    for row in rows_for_generation:
        if row.get("parse_status") not in common.SUCCESS_STATUSES:
            continue
        content_hash = str(row.get("extracted_hash") or row.get("source_hash") or "")
        if not content_hash or content_hash in by_hash:
            continue
        by_hash[content_hash] = row
    items = list(by_hash.items())
    if args.limit:
        items = items[:args.limit]

    direct: list[dict] = []
    long_documents: list[tuple[dict, list[str]]] = []
    cached = 0
    for content_hash, row in items:
        path = content_profile_path(paths, content_hash)
        if not args.no_ai_cache and valid_profile(path, content_hash, schema_hash, provider=provider):
            cached += 1
            continue
        extracted = paths["extracted"] / f"{row['source_id']}.txt"
        if not extracted.exists():
            raise RuntimeError(f"已准入且标记解析成功，但缺少文本抽取结果：{row['source_id']}")
        original_content = extracted.read_text(encoding="utf-8")
        content, redaction_warnings = common.redact_sensitive(original_content)
        content_mode = "full_text" if content.strip() else "empty_text"
        task = {
            "document_key": content_hash,
            "title": row.get("file_name", ""),
            "source_path": row.get("source_path", ""),
            "extension": row.get("extension", ""),
            "content_mode": content_mode,
            "content": content,
            "redaction_warnings": redaction_warnings,
            "row": row,
        }
        chunks = split_text(content)
        if len(chunks) == 1:
            direct.append(task)
        else:
            long_documents.append((task, chunks))

    generated: dict[str, dict] = {}
    if direct:
        generated.update(process_batches(
            pack_tasks(direct, REQUEST_CHARS, MAX_ITEMS), workers=args.workers, paths=paths,
            schema_path=schema_path, schema_hash=schema_hash, model=model,
            provider=provider, api_key_env=api_key_env,
        ))

    for task, chunks in long_documents:
        chunk_tasks: list[dict] = []
        for index, chunk in enumerate(chunks, start=1):
            chunk_key = f"{task['document_key']}:chunk:{index:04d}"
            chunk_path = paths["chunk_profiles"] / f"{common.sha256_text(chunk_key)}.json"
            existing = common.load_json(chunk_path)
            if isinstance(existing, dict) and existing.get("_chunk_input_hash") == common.sha256_text(chunk):
                continue
            chunk_tasks.append({
                **task,
                "document_key": chunk_key,
                "content_mode": f"full_text_chunk_{index}_of_{len(chunks)}",
                "content": chunk,
                "chunk_path": chunk_path,
            })
        if chunk_tasks:
            chunk_results = process_batches(
                pack_tasks(chunk_tasks, REQUEST_CHARS, MAX_ITEMS), workers=args.workers, paths=paths,
                schema_path=schema_path, schema_hash=schema_hash, model=model,
                provider=provider, api_key_env=api_key_env,
            )
            for chunk_task in chunk_tasks:
                item = chunk_results[chunk_task["document_key"]]
                item["_chunk_input_hash"] = common.sha256_text(chunk_task["content"])
                common.write_json(chunk_task["chunk_path"], item)
        profiles: list[dict] = []
        for index, chunk in enumerate(chunks, start=1):
            chunk_key = f"{task['document_key']}:chunk:{index:04d}"
            chunk_path = paths["chunk_profiles"] / f"{common.sha256_text(chunk_key)}.json"
            profile = common.load_json(chunk_path)
            if not isinstance(profile, dict):
                raise RuntimeError(f"缺少片段画像：{chunk_key}")
            profiles.append({key: value for key, value in profile.items() if not key.startswith("_")})
        rollup_task = {
            **task,
            "content_mode": "chunk_profiles_rollup",
            "content": json.dumps(profiles, ensure_ascii=False),
        }
        generated.update(run_batch(
            [rollup_task], paths=paths, schema_path=schema_path, schema_hash=schema_hash,
            model=model, provider=provider, api_key_env=api_key_env, rollup=True,
        ))

    for content_hash, item in generated.items():
        row = by_hash[content_hash]
        content = (paths["extracted"] / f"{row['source_id']}.txt").read_text(encoding="utf-8") if (paths["extracted"] / f"{row['source_id']}.txt").exists() else ""
        chunks = split_text(content) if content else []
        item["_meta"] = {
            "input_hash": content_hash,
            "prompt_version": common.PROMPT_VERSION,
            "prompt_hash": common.sha256_text(semantic_prompt([{**row, "document_key": "<KEY>", "content_mode": "<MODE>", "content": "<CONTENT>"}])),
            "schema_hash": schema_hash,
            "codex_cli_version": llm_providers.backend_tag(provider),
            "model": model or "default",
            "provider": provider,
            "generated_at": common.now_iso(),
            "content_mode": "chunk_profiles_rollup" if len(chunks) > 1 else "full_text",
            "input_chars": len(content),
            "chunks": len(chunks),
            "attempts": 1,
        }
        generated_task = next((task for task in direct if task["document_key"] == content_hash), None)
        if generated_task is None:
            generated_task = next((task for task, _ in long_documents if task["document_key"] == content_hash), None)
        redactions = (generated_task or {}).get("redaction_warnings", [])
        if redactions:
            notice = "输入在送入 Codex 前已脱敏：" + "、".join(redactions)
            if notice not in item["warnings"]:
                item["warnings"].append(notice)
        common.write_json(content_profile_path(paths, content_hash), item)

    ledger = build_source_profiles(rows, paths, schema_hash)
    report = (
        "# Codex 语义蒸馏进度\n\n"
        f"- 唯一内容：{len(by_hash)}\n"
        f"- 本次选择：{len(items)}\n"
        f"- 命中有效缓存：{cached}\n"
        f"- 本次新生成：{len(generated)}\n"
        f"- 已生成来源画像：{len(ledger)}\n"
        f"- 提示词版本：{common.PROMPT_VERSION}\n"
        f"- Schema SHA-256：{schema_hash}\n"
    )
    common.atomic_write(paths["reports"] / "codex-semantic-progress.md", report)
    print(f"CODEX_SEMANTIC_OK selected={len(items)} cached={cached} generated={len(generated)} source_profiles={len(ledger)}")


if __name__ == "__main__":
    main()
