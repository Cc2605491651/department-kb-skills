#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_common as common


RULE_STRENGTH = {
    "R01": "strong", "R02": "strong", "R03": "strong",
    "R04": "medium", "R05": "medium", "R06": "medium", "R07": "medium", "R08": "medium",
    "R09": "weak", "R10": "weak",
}
RULE_RELATION = {
    "R01": "完全重复", "R02": "明确引用", "R03": "明确引用", "R04": "制度依据/引用",
    "R05": "版本关系", "R06": "同业务对象", "R07": "上下游", "R08": "实施关系",
    "R09": "同主题", "R10": "路径上下文",
}
STABLE_ID_RE = re.compile(r"\b[A-Z][A-Z0-9_-]{1,20}-[A-F0-9]{12}\b", re.I)
NODE_LINK_RE = re.compile(r"https://alidocs\.dingtalk\.com/(?:i/)?(?:nodes|spaces)/([A-Za-z0-9_-]+)", re.I)
TITLE_REF_RE = re.compile(r"(?:依据|参照|按照|详见|见|引用|遵循)\s*[《“\"]([^》”\"\n]{2,100})[》”\"]")
VERSION_RE = re.compile(r"(?i)新版|旧版|最新版|修订|替代|取代|废止|失效|沿用|v\s*\d+(?:\.\d+)*|version\s*\d+(?:\.\d+)*")
BUSINESS_ID_RES = [
    re.compile(r"\bPRJ[-_ ]?\d{3,}[A-Z]?\b", re.I),
    re.compile(r"\b(?:TICKET|WORK|BUG|REQ)[-_]?\d{3,}\b", re.I),
    re.compile(r"\b[A-Z]{2,10}[-_/]\d{3,}(?:[-_/]\d+)?\b"),
]
ROLE_PAIRS = {
    ("制度", "流程"), ("制度", "模板"), ("制度", "案例"),
    ("规范", "执行流程"), ("规范", "模板"), ("流程", "模板"),
    ("流程", "案例"), ("方案", "交付物"), ("输入", "执行流程"),
}
FIELDS = [
    "relation_id", "source_id", "source_title", "source_url", "target_id", "target_title", "target_url",
    "proposed_type", "direction", "rule_ids", "strengths", "strong_count", "medium_count", "weak_count",
    "local_score", "local_evidence", "gate_status", "preliminary_review_level", "risk_flags", "candidate_status",
]


def read_profiles(paths: dict[str, Path]) -> tuple[list[dict], dict[str, dict]]:
    rows: list[dict] = []
    for path in sorted(paths["source_profiles"].glob("*.json")):
        value = common.load_json(path)
        if isinstance(value, dict) and value.get("source_id"):
            rows.append(value)
    return rows, {row["source_id"]: row for row in rows}


def source_text(profile: dict, paths: dict[str, Path]) -> str:
    path = paths["extracted"] / f"{profile['source_id']}.txt"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def add_rule(store: dict, source_id: str, target_id: str, rule: str, evidence: str, *, direction: str = "待核验", score: float = 0.0) -> None:
    if not source_id or not target_id or source_id == target_id:
        return
    if direction == "双向":
        source_id, target_id = sorted((source_id, target_id))
    key = (source_id, target_id)
    candidate = store.setdefault(key, {"source_id": source_id, "target_id": target_id, "rules": {}, "direction": direction, "score": 0.0})
    candidate["rules"].setdefault(rule, [])
    if evidence not in candidate["rules"][rule]:
        candidate["rules"][rule].append(evidence[:800])
    candidate["score"] = max(float(candidate.get("score") or 0), score)
    if candidate["direction"] == "待核验" and direction != "待核验":
        candidate["direction"] = direction


def business_ids(value: str) -> set[str]:
    result: set[str] = set()
    for pattern in BUSINESS_ID_RES:
        result.update(match.group(0).upper().replace(" ", "") for match in pattern.finditer(value))
    return result


def profile_terms(profile: dict) -> str:
    content = profile.get("content_profile") or {}
    fields = [
        profile.get("file_name", ""), content.get("summary", ""), content.get("core_theme", ""),
        " ".join(content.get("keywords") or []), " ".join(content.get("business_objects") or []),
        " ".join(content.get("scenarios") or []),
    ]
    return "\n".join(str(value) for value in fields)


def tfidf_candidates(profiles: list[dict], top_k: int = 10) -> list[tuple[str, str, float]]:
    tokens_by_id: dict[str, Counter] = {}
    document_frequency: Counter = Counter()
    for profile in profiles:
        counts = Counter(common.tokenise(profile_terms(profile)))
        counts = Counter({token: count for token, count in counts.items() if len(token) >= 2})
        tokens_by_id[profile["source_id"]] = counts
        document_frequency.update(counts.keys())
    total = max(1, len(profiles))
    inverted: dict[str, list[tuple[str, float]]] = defaultdict(list)
    norms: dict[str, float] = {}
    for source_id, counts in tokens_by_id.items():
        weights: dict[str, float] = {}
        for token, count in counts.items():
            idf = math.log((1 + total) / (1 + document_frequency[token])) + 1
            weights[token] = (1 + math.log(count)) * idf
        norms[source_id] = math.sqrt(sum(value * value for value in weights.values())) or 1.0
        for token, weight in weights.items():
            inverted[token].append((source_id, weight))
    results: list[tuple[str, str, float]] = []
    for source_id, counts in tokens_by_id.items():
        scores: defaultdict[str, float] = defaultdict(float)
        for token, count in counts.items():
            idf = math.log((1 + total) / (1 + document_frequency[token])) + 1
            weight = (1 + math.log(count)) * idf
            for target_id, target_weight in inverted[token]:
                if target_id != source_id:
                    scores[target_id] += weight * target_weight
        ranked = sorted(
            ((target, score / (norms[source_id] * norms[target])) for target, score in scores.items()),
            key=lambda pair: pair[1], reverse=True,
        )[:top_k]
        results.extend((source_id, target, score) for target, score in ranked if score >= 0.12)
    return results


def candidate_rows(profiles: list[dict], by_id: dict[str, dict], paths: dict[str, Path], max_per_document: int) -> list[dict]:
    store: dict[tuple[str, str], dict] = {}
    text_by_id = {profile["source_id"]: source_text(profile, paths) for profile in profiles}
    node_map = {str(profile.get("node_id") or ""): profile["source_id"] for profile in profiles if profile.get("node_id")}
    stable_map = {profile["source_id"].upper(): profile["source_id"] for profile in profiles}
    titles: defaultdict[str, list[str]] = defaultdict(list)
    for profile in profiles:
        normalized = common.normalized_title(str(profile.get("file_name") or ""))
        if normalized:
            titles[normalized].append(profile["source_id"])

    # R01: exact content equality.
    hash_groups: defaultdict[str, list[str]] = defaultdict(list)
    for profile in profiles:
        value = str(profile.get("content_profile_hash") or "")
        if value:
            hash_groups[value].append(profile["source_id"])
    for content_hash, ids in hash_groups.items():
        if len(ids) < 2:
            continue
        for index, source_id in enumerate(sorted(ids)):
            for target_id in sorted(ids)[index + 1:]:
                add_rule(store, source_id, target_id, "R01", f"规范化内容 SHA-256 一致：{content_hash}", direction="双向", score=1.0)

    ids_by_business: defaultdict[str, list[str]] = defaultdict(list)
    objects: defaultdict[str, list[str]] = defaultdict(list)
    inputs: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)
    paths_map: defaultdict[str, list[str]] = defaultdict(list)
    for profile in profiles:
        source_id = profile["source_id"]
        text = text_by_id[source_id]
        combined = f"{profile.get('file_name', '')}\n{text}"
        for match in NODE_LINK_RE.finditer(text):
            target_id = node_map.get(match.group(1))
            if target_id:
                add_rule(store, source_id, target_id, "R02", f"正文包含目标节点链接：{match.group(0).split('?')[0]}", direction="来源→目标", score=1.0)
        for match in STABLE_ID_RE.finditer(text):
            target_id = stable_map.get(match.group(0).upper())
            if target_id:
                add_rule(store, source_id, target_id, "R03", f"正文出现稳定 ID：{match.group(0)}", direction="来源→目标", score=1.0)
        for match in TITLE_REF_RE.finditer(text):
            title = match.group(1).strip()
            target_ids = titles.get(common.normalized_title(title), [])
            if len(target_ids) == 1:
                add_rule(store, source_id, target_ids[0], "R04", f"标题引用：{match.group(0)[:200]}", direction="来源→目标", score=0.72)
        for business_id in business_ids(combined):
            ids_by_business[business_id].append(source_id)
        content = profile.get("content_profile") or {}
        for value in content.get("business_objects") or []:
            key = common.normalized_text(str(value))
            if len(key) >= 2:
                objects[key].append(source_id)
        for value in content.get("inputs") or []:
            key = common.normalized_text(str(value))
            if len(key) >= 2:
                inputs[key].append((source_id, str(value)))
        parent = str(Path(str(profile.get("source_path") or "")).parent)
        if parent and parent != ".":
            paths_map[parent].append(source_id)

    # R05: same normalized title subject plus version signals.
    for title_key, ids in titles.items():
        if len(title_key) < 4 or len(ids) < 2 or len(ids) > 30:
            continue
        versioned = any(VERSION_RE.search(str(by_id[source_id].get("file_name") or "")) or (by_id[source_id].get("content_profile") or {}).get("version_signals") for source_id in ids)
        if not versioned:
            continue
        for index, source_id in enumerate(sorted(ids)):
            for target_id in sorted(ids)[index + 1:]:
                add_rule(store, source_id, target_id, "R05", f"标题主体一致：{title_key}", direction="待核验", score=0.75)

    # R06: exact business identifiers.
    for business_id, ids in ids_by_business.items():
        unique = sorted(set(ids))
        if len(unique) < 2 or len(unique) > 50:
            continue
        for index, source_id in enumerate(unique):
            for target_id in unique[index + 1:]:
                add_rule(store, source_id, target_id, "R06", f"相同业务编号：{business_id}", direction="双向", score=0.68)

    # R07: declared output of A equals declared input of B.
    for profile in profiles:
        source_id = profile["source_id"]
        for output in (profile.get("content_profile") or {}).get("outputs") or []:
            key = common.normalized_text(str(output))
            if len(key) < 2:
                continue
            for target_id, target_input in inputs.get(key, []):
                add_rule(store, source_id, target_id, "R07", f"产出“{output}”匹配目标输入“{target_input}”", direction="来源→目标", score=0.74)

    # R08: shared business object and complementary roles.
    for object_key, ids in objects.items():
        unique = sorted(set(ids))
        if len(unique) < 2 or len(unique) > 40:
            continue
        for index, source_id in enumerate(unique):
            source_role = str((by_id[source_id].get("content_profile") or {}).get("document_role") or (by_id[source_id].get("content_profile") or {}).get("page_type_candidate") or "")
            for target_id in unique[index + 1:]:
                target_role = str((by_id[target_id].get("content_profile") or {}).get("document_role") or (by_id[target_id].get("content_profile") or {}).get("page_type_candidate") or "")
                compatible = any(a in source_role and b in target_role or b in source_role and a in target_role for a, b in ROLE_PAIRS)
                if compatible:
                    add_rule(store, source_id, target_id, "R08", f"业务对象“{object_key}”相同，角色互补：{source_role} / {target_role}", direction="待核验", score=0.66)

    # R09: sparse TF-IDF cosine recall.
    for source_id, target_id, score in tfidf_candidates(profiles):
        add_rule(store, source_id, target_id, "R09", f"TF-IDF 余弦相似度={score:.4f}", direction="双向", score=score)

    # R10: same immediate topic/project directory; cap large generic folders.
    for parent, ids in paths_map.items():
        unique = sorted(set(ids))
        if len(unique) < 2 or len(unique) > 50:
            continue
        for index, source_id in enumerate(unique):
            for target_id in unique[index + 1:]:
                add_rule(store, source_id, target_id, "R10", f"相同直接目录：{parent}", direction="双向", score=0.2)

    rows: list[dict] = []
    per_source: defaultdict[str, int] = defaultdict(int)
    ranked: list[tuple[tuple, dict]] = []
    for candidate in store.values():
        rules = sorted(candidate["rules"])
        strengths = [RULE_STRENGTH[rule] for rule in rules]
        strong_count = strengths.count("strong")
        medium_count = strengths.count("medium")
        weak_count = strengths.count("weak")
        eligible = strong_count >= 1 or medium_count >= 2
        preliminary = "L0" if "R01" in rules else "L1" if any(rule in rules for rule in ("R02", "R03")) else "L2"
        risk_flags: list[str] = []
        evidence_text = json.dumps(candidate["rules"], ensure_ascii=False)
        if VERSION_RE.search(evidence_text) and re.search(r"废止|失效|替代|取代", evidence_text):
            preliminary = "L3"
            risk_flags.append("制度效力或版本替代")
        score = strong_count * 100 + medium_count * 20 + weak_count * 2 + float(candidate.get("score") or 0)
        ranked.append(((-int(eligible), -strong_count, -medium_count, -score, candidate["source_id"], candidate["target_id"]), {
            **candidate,
            "rule_ids": rules,
            "strengths": strengths,
            "strong_count": strong_count,
            "medium_count": medium_count,
            "weak_count": weak_count,
            "local_score": round(score, 4),
            "gate_status": "eligible" if eligible else "insufficient_evidence",
            "preliminary_review_level": preliminary,
            "risk_flags": risk_flags,
        }))
    for _, candidate in sorted(ranked, key=lambda item: item[0]):
        source_id, target_id = candidate["source_id"], candidate["target_id"]
        if per_source[source_id] >= max_per_document or per_source[target_id] >= max_per_document:
            continue
        per_source[source_id] += 1
        per_source[target_id] += 1
        source, target = by_id[source_id], by_id[target_id]
        rules = candidate["rule_ids"]
        relation_id = "REL-" + hashlib.sha256(f"{source_id}|{target_id}|{'/'.join(rules)}".encode()).hexdigest()[:16].upper()
        proposed = RULE_RELATION[rules[0]] if len(rules) == 1 else "/".join(dict.fromkeys(RULE_RELATION[rule] for rule in rules))
        rows.append({
            "relation_id": relation_id,
            "source_id": source_id,
            "source_title": source.get("file_name", ""),
            "source_url": source.get("source_url", ""),
            "target_id": target_id,
            "target_title": target.get("file_name", ""),
            "target_url": target.get("source_url", ""),
            "proposed_type": proposed,
            "direction": candidate["direction"],
            "rule_ids": json.dumps(rules, ensure_ascii=False),
            "strengths": json.dumps(candidate["strengths"], ensure_ascii=False),
            "strong_count": candidate["strong_count"],
            "medium_count": candidate["medium_count"],
            "weak_count": candidate["weak_count"],
            "local_score": candidate["local_score"],
            "local_evidence": json.dumps(candidate["rules"], ensure_ascii=False),
            "gate_status": candidate["gate_status"],
            "preliminary_review_level": candidate["preliminary_review_level"],
            "risk_flags": json.dumps(candidate["risk_flags"], ensure_ascii=False),
            "candidate_status": "待Codex核验" if candidate["gate_status"] == "eligible" and "R01" not in rules else "已确定性核验" if "R01" in rules else "证据不足-仅保留召回",
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Build R01-R10 deterministic relation candidates.")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--max-per-document", type=int, default=30)
    args = parser.parse_args()
    paths = common.job_paths(args.job)
    profiles, by_id = read_profiles(paths)
    if not profiles:
        raise SystemExit("没有来源画像；请先运行 codex_semantic.py")
    rows = candidate_rows(profiles, by_id, paths, args.max_per_document)
    common.write_csv(paths["ledgers"] / "relation-candidates.csv", rows, FIELDS)
    common.atomic_write(paths["ledgers"] / "relation-candidates.jsonl", "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    counts = Counter()
    for row in rows:
        for rule in json.loads(row["rule_ids"]):
            counts[rule] += 1
    report = "# 本地关系候选召回\n\n" + f"- 来源画像：{len(profiles)}\n- 候选边：{len(rows)}\n- 满足核验门槛：{sum(row['gate_status'] == 'eligible' for row in rows)}\n" + "\n".join(f"- {rule}：{counts[rule]}" for rule in sorted(counts)) + "\n"
    common.atomic_write(paths["reports"] / "relation-candidate-progress.md", report)
    print(f"RELATION_CANDIDATES_OK profiles={len(profiles)} candidates={len(rows)} eligible={sum(row['gate_status'] == 'eligible' for row in rows)}")


if __name__ == "__main__":
    main()
