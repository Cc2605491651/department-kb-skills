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
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_common as common
import llm_providers


FIELDS = [
    "relation_id", "source_id", "source_title", "source_url", "target_id", "target_title", "target_url",
    "rule_ids", "local_evidence", "verification_status", "verified_relation_type", "direction", "relation_meaning",
    "source_evidence", "target_evidence", "confidence", "review_level", "risk_flags", "verification_reason",
    "coverage_mode", "review_status", "prompt_version", "input_hash", "model", "verified_at",
]
BUNDLED_SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "relation-verification-v1.json"


def load_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def load_profile(paths: dict[str, Path], source_id: str) -> dict:
    value = common.load_json(paths["source_profiles"] / f"{source_id}.json")
    if not isinstance(value, dict):
        raise RuntimeError(f"来源画像不存在：{source_id}")
    return value


def evidence_text(text: str, queries: list[str], limit: int = 55_000) -> tuple[str, str]:
    if len(text) <= limit:
        return text, "full_text"
    windows: list[tuple[int, int]] = []
    lowered = text.casefold()
    for query in queries:
        query = query.strip()
        if len(query) < 2:
            continue
        start = 0
        needle = query.casefold()
        while len(windows) < 20:
            index = lowered.find(needle, start)
            if index < 0:
                break
            windows.append((max(0, index - 1600), min(len(text), index + len(query) + 2400)))
            start = index + len(query)
    windows.extend([(0, min(len(text), 5000)), (max(0, len(text) - 3000), len(text))])
    windows.sort()
    merged: list[tuple[int, int]] = []
    for start, end in windows:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    parts: list[str] = []
    chars = 0
    for start, end in merged:
        part = f"[[CHAR {start}:{end}]]\n{text[start:end]}"
        if chars + len(part) > limit:
            break
        parts.append(part)
        chars += len(part)
    return "\n\n".join(parts), "retrieved_sections_from_full_text"


def build_task(candidate: dict, paths: dict[str, Path]) -> dict:
    source = load_profile(paths, candidate["source_id"])
    target = load_profile(paths, candidate["target_id"])
    local_evidence = json.loads(candidate.get("local_evidence") or "{}")
    queries = [candidate.get("target_title", ""), candidate.get("source_title", ""), candidate["target_id"], candidate["source_id"]]
    for values in local_evidence.values():
        queries.extend(str(value)[:100] for value in values)
    source_path = paths["extracted"] / f"{candidate['source_id']}.txt"
    target_path = paths["extracted"] / f"{candidate['target_id']}.txt"
    source_raw = source_path.read_text(encoding="utf-8", errors="replace") if source_path.exists() else ""
    target_raw = target_path.read_text(encoding="utf-8", errors="replace") if target_path.exists() else ""
    source_redacted, source_redactions = common.redact_sensitive(source_raw)
    target_redacted, target_redactions = common.redact_sensitive(target_raw)
    source_text, source_mode = evidence_text(source_redacted, queries)
    target_text, target_mode = evidence_text(target_redacted, queries)
    input_hash = common.sha256_text(json.dumps({
        "candidate": candidate,
        "source_hash": source.get("content_profile_hash"),
        "target_hash": target.get("content_profile_hash"),
        "source_text_hash": common.sha256_text(source_raw),
        "target_text_hash": common.sha256_text(target_raw),
        "prompt_version": common.RELATION_PROMPT_VERSION,
    }, ensure_ascii=False, sort_keys=True))
    return {
        "relation_id": candidate["relation_id"],
        "candidate": candidate,
        "source_profile": source,
        "target_profile": target,
        "source_text": source_text,
        "target_text": target_text,
        "coverage_mode": f"source={source_mode};target={target_mode}",
        "redaction_warnings": sorted(set(source_redactions + target_redactions)),
        "input_hash": input_hash,
    }


def prompt(tasks: list[dict]) -> str:
    payload = []
    for task in tasks:
        payload.append({
            "relation_id": task["relation_id"],
            "candidate": task["candidate"],
            "source_profile": task["source_profile"],
            "target_profile": task["target_profile"],
            "source_evidence_text": task["source_text"],
            "target_evidence_text": task["target_text"],
            "coverage_mode": task["coverage_mode"],
            "redaction_warnings": task.get("redaction_warnings", []),
        })
    return f"""你正在核验部门知识库的文档关系候选。逐项阅读双方画像、本地规则证据和证据原文，保持 relation_id 不变。

判定要求：
1. 关系必须能在双方内容或明确引用中复核；仅同目录或文本相似不能确认关系。
2. 区分“同主题”与真正的版本、引用、制度依据、上下游、实施、模板/案例关系。
3. 给出明确方向；双向关系写“双向”，上下游写“来源→目标”或“目标→来源”。
4. relation_meaning 用一句话说明双方如何关联，不复述标题。
5. source_evidence/target_evidence 写可定位的短证据；没有一端证据就返回空数组，并降低置信度。
6. L1 仅限明确链接/稳定ID引用且不存在业务风险；普通上下游、实施、模板应用、普通依据为 L2；冲突、废止/替代效力、权限泄露风险，或关系会改变制度是否有效/谁优先/是否强制时必须标为 L3。
7. 证据不足返回 rejected；存在冲突或无法确定方向返回 needs_review，禁止猜测。
8. 每项必须输出：即使判定 rejected 或 needs_review，也必须为每个 relation_id 输出一项，不得省略任何候选；relation_id 必须与输入完全一致，不得改写或截断。
9. 输出是正式台账，不得出现“老大、老板、用户、您好”等对话称呼；不得复述密码、口令、Token、API Key 或私钥。

输入 JSON：
{json.dumps(payload, ensure_ascii=False)}
"""


def cache_path(paths: dict[str, Path], relation_id: str) -> Path:
    return paths["semantic"] / "relation-verifications" / f"{relation_id}.json"


def valid_cache(paths: dict[str, Path], task: dict, schema_hash: str, provider: str = "") -> dict | None:
    value = common.load_json(cache_path(paths, task["relation_id"]))
    if not isinstance(value, dict):
        return None
    meta = value.get("_meta") or {}
    if meta.get("input_hash") != task["input_hash"] or meta.get("schema_hash") != schema_hash or meta.get("prompt_version") != common.RELATION_PROMPT_VERSION:
        return None
    if meta.get("provider", "") != provider:
        return None
    return value


def run_batch(tasks: list[dict], paths: dict[str, Path], schema_path: Path, schema_hash: str, model: str, provider: str = "", api_key_env: str = "") -> dict[str, dict]:
    rendered = prompt(tasks)
    try:
        payload, metadata = llm_providers.run_llm_structured(prompt=rendered, schema_path=schema_path, cwd=paths["job"], provider=provider, model=model, api_key_env=api_key_env)
        items = payload.get("items") or []
        by_id = {item.get("relation_id"): item for item in items if isinstance(item, dict)}
        if set(by_id) != {task["relation_id"] for task in tasks}:
            raise RuntimeError("Codex 关系输出 ID 集合不匹配")
        results: dict[str, dict] = {}
        for task in tasks:
            item = by_id[task["relation_id"]]
            serialized = json.dumps(item, ensure_ascii=False)
            if re.search(r"(?:^|[\"：:])(?:老大|老板|用户|您好)[，,：:\s]", serialized):
                raise RuntimeError("Codex 关系输出包含对话称呼")
            if re.search(r"(?i)(?:密码|password|passcode)\s*[:：=]\s*(?!\[REDACTED\])[^\s,，;；。]{3,}", serialized):
                raise RuntimeError("Codex 关系输出复述了敏感凭据")
            item["_meta"] = {
                **metadata, "input_hash": task["input_hash"], "schema_hash": schema_hash,
                "prompt_version": common.RELATION_PROMPT_VERSION, "prompt_hash": common.sha256_text(rendered),
                "provider": metadata.get("provider", provider),
                "coverage_mode": task["coverage_mode"],
            }
            common.write_json(cache_path(paths, task["relation_id"]), item)
            results[task["relation_id"]] = item
        common.append_jsonl(paths["ledgers"] / "codex-task-log.jsonl", {
            "task_id": "RELVERIFY-" + hashlib.sha256(rendered.encode()).hexdigest()[:16],
            "task_kind": "relation_verify", "document_keys": [task["relation_id"] for task in tasks],
            "input_hash": common.sha256_text(rendered), "prompt_version": common.RELATION_PROMPT_VERSION,
            "schema_hash": schema_hash, "input_chars": len(rendered), "output_chars": len(json.dumps(payload, ensure_ascii=False)),
            "status": "success", "error": "", **metadata,
        })
        return results
    except Exception as error:
        common.append_jsonl(paths["ledgers"] / "codex-task-log.jsonl", {
            "task_id": "RELVERIFY-" + hashlib.sha256(rendered.encode()).hexdigest()[:16], "task_kind": "relation_verify",
            "document_keys": [task["relation_id"] for task in tasks], "input_hash": common.sha256_text(rendered),
            "prompt_version": common.RELATION_PROMPT_VERSION, "schema_hash": schema_hash, "input_chars": len(rendered),
            "output_chars": 0, "status": "failed", "error": common.safe_error(error), "finished_at": common.now_iso(), "model": model or "default",
        })
        if len(tasks) > 1:
            middle = len(tasks) // 2
            return {
                **run_batch(tasks[:middle], paths, schema_path, schema_hash, model, provider=provider, api_key_env=api_key_env),
                **run_batch(tasks[middle:], paths, schema_path, schema_hash, model, provider=provider, api_key_env=api_key_env),
            }
        raise


def to_row(candidate: dict, verification: dict) -> dict:
    rules = json.loads(candidate.get("rule_ids") or "[]")
    meta = verification.get("_meta") or {}
    if "R01" in rules:
        status = "confirmed"
        relation_type = "完全重复"
        direction = "双向"
        meaning = "双方规范化完整内容的 SHA-256 一致。"
        review_level = "L0"
        confidence = 1.0
        source_evidence = ["规范化完整内容 SHA-256 一致"]
        target_evidence = ["规范化完整内容 SHA-256 一致"]
        risk_flags: list[str] = []
        reason = "R01 确定性规则自动确认"
        coverage = "deterministic_hash"
        review_status = "已自动确认"
    else:
        status = verification.get("verification_status", "needs_review")
        relation_type = verification.get("relation_type", candidate.get("proposed_type", ""))
        direction = verification.get("direction", candidate.get("direction", "待核验"))
        meaning = verification.get("relation_meaning", "")
        review_level = verification.get("review_level", candidate.get("preliminary_review_level", "L2"))
        confidence = verification.get("confidence", 0)
        source_evidence = verification.get("source_evidence", [])
        target_evidence = verification.get("target_evidence", [])
        risk_flags = verification.get("risk_flags", [])
        reason = verification.get("reason", "")
        coverage = meta.get("coverage_mode", "")
        if status == "confirmed" and review_level in {"L1", "L2"}:
            review_status = "L2自动通过待抽样" if review_level == "L2" else "自动确认待发布"
        elif status in {"confirmed", "needs_review"}:
            review_status = "L3待负责人确认"
        else:
            review_status = "已驳回-模型核验"
    return {
        "relation_id": candidate["relation_id"], "source_id": candidate["source_id"], "source_title": candidate["source_title"], "source_url": candidate["source_url"],
        "target_id": candidate["target_id"], "target_title": candidate["target_title"], "target_url": candidate["target_url"],
        "rule_ids": candidate["rule_ids"], "local_evidence": candidate["local_evidence"], "verification_status": status,
        "verified_relation_type": relation_type, "direction": direction, "relation_meaning": meaning,
        "source_evidence": json.dumps(source_evidence, ensure_ascii=False), "target_evidence": json.dumps(target_evidence, ensure_ascii=False),
        "confidence": confidence, "review_level": review_level, "risk_flags": json.dumps(risk_flags, ensure_ascii=False),
        "verification_reason": reason, "coverage_mode": coverage, "review_status": review_status,
        "prompt_version": common.RELATION_PROMPT_VERSION, "input_hash": meta.get("input_hash", ""),
        "model": meta.get("model", "deterministic" if "R01" in rules else "default"), "verified_at": meta.get("finished_at", common.now_iso()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify eligible relation candidates with Codex or third-party LLM.")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--model", default="")
    parser.add_argument("--llm-provider", default="", help="codex|siliconflow|kimi，默认 codex")
    parser.add_argument("--llm-model", default="", help="第三方实际模型名；为空用 provider 默认模型")
    parser.add_argument("--llm-api-key-env", default="", help="第三方 API key 所在环境变量名")
    parser.add_argument("--no-ai-cache", action="store_true", help="跳过核验缓存，每次真实调用")
    args = parser.parse_args()
    # resolve 返回第三项是 key 值，只做校验不沿用；后续一律用 args.llm_api_key_env（环境变量名）传递
    llm_provider = args.llm_provider or str(common.load_task_config(args.job).get("llm.provider", "provider", default="") or "")
    if not llm_provider:
        raise RuntimeError("必须显式声明 LLM 提供方：配置 task-config.yaml 的 llm.provider 或传 --llm-provider")
    provider, model, _ = llm_providers.resolve(llm_provider, args.llm_model or args.model, args.llm_api_key_env)
    if not llm_providers.is_codex(provider) and not os.environ.get(args.llm_api_key_env or llm_providers.default_key_env(provider), ""):
        raise RuntimeError(f"第三方 LLM 缺少 API key：请先设置环境变量 {args.llm_api_key_env or llm_providers.default_key_env(provider)}")
    api_key_env = args.llm_api_key_env
    # 第三方 json_object 输出不稳定（ID 改写/漏项），每批只放 1 个候选降低失败率；codex 保持 5 个
    batch_max = 1 if not llm_providers.is_codex(provider) else 5
    paths = common.job_paths(args.job)
    candidate_path = paths["ledgers"] / "relation-candidates.csv"
    if not candidate_path.exists():
        raise SystemExit("缺少 relation-candidates.csv；请先运行 build_relation_candidates.py")
    candidates = load_csv(candidate_path)
    eligible = [row for row in candidates if row.get("gate_status") == "eligible"]
    if args.limit:
        eligible = eligible[:args.limit]
    schema_path = paths["schemas"] / "relation-verification-v1.json"
    if not BUNDLED_SCHEMA.exists():
        raise RuntimeError(f"Skill缺少关系核验Schema：{BUNDLED_SCHEMA}")
    common.atomic_write(schema_path, BUNDLED_SCHEMA.read_bytes())
    schema_hash = common.sha256_bytes(schema_path.read_bytes())
    tasks: list[dict] = []
    results: dict[str, dict] = {}
    for candidate in eligible:
        if "R01" in json.loads(candidate.get("rule_ids") or "[]"):
            results[candidate["relation_id"]] = {"verification_status": "confirmed", "_meta": {"model": "deterministic", "finished_at": common.now_iso()}}
            continue
        task = build_task(candidate, paths)
        cached = None if args.no_ai_cache else valid_cache(paths, task, schema_hash, provider=provider)
        if cached:
            results[candidate["relation_id"]] = cached
        else:
            tasks.append(task)
    batches: list[list[dict]] = []
    current: list[dict] = []
    chars = 0
    for task in tasks:
        size = len(task["source_text"]) + len(task["target_text"])
        if current and (len(current) >= batch_max or chars + size > 115_000):
            batches.append(current)
            current, chars = [], 0
        current.append(task)
        chars += size
    if current:
        batches.append(current)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(run_batch, batch, paths, schema_path, schema_hash, model, provider=provider, api_key_env=api_key_env) for batch in batches]
        for future in concurrent.futures.as_completed(futures):
            try:
                results.update(future.result())
            except RuntimeError as error:
                print(f"RELATION_BATCH_FAILED {common.safe_error(error)}", flush=True)
            print(f"RELATION_VERIFY_PROGRESS completed={len(results)}/{len(eligible)}", flush=True)
    rows = [to_row(candidate, results[candidate["relation_id"]]) for candidate in eligible if candidate["relation_id"] in results]
    common.write_csv(paths["ledgers"] / "relation-verification.csv", rows, FIELDS)
    # This ledger is the publish gate input. L2 auto-passes with sampling; L3 remains pending.
    common.write_csv(paths["ledgers"] / "relation-ledger.csv", rows, FIELDS)
    report = (
        "# Codex 关系核验进度\n\n"
        f"- 满足本地门槛：{len(eligible)}\n- 已核验：{len(rows)}\n"
        f"- L0：{sum(row['review_level'] == 'L0' for row in rows)}\n"
        f"- L1：{sum(row['review_level'] == 'L1' for row in rows)}\n"
        f"- L2：{sum(row['review_level'] == 'L2' for row in rows)}\n"
        f"- L3：{sum(row['review_level'] == 'L3' for row in rows)}\n"
        f"- 待业务确认：{sum(row['review_status'] == 'L3待负责人确认' for row in rows)}\n"
    )
    common.atomic_write(paths["reports"] / "relation-verification-progress.md", report)
    print(f"RELATION_VERIFY_OK eligible={len(eligible)} verified={len(rows)} pending_business={sum(row['review_status'] == 'L3待负责人确认' for row in rows)}")


if __name__ == "__main__":
    main()
