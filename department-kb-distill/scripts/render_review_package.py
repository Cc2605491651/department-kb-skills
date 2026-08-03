#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_common as common


STANDARD_METADATA_FIELDS = (
    "title", "page_type", "scenario", "keywords", "summary", "owner", "status",
    "processing", "version", "source", "source_updated_at", "content_hash", "sources",
    "blindspots", "relations", "property_generated_at", "property_updated_at",
)
PAGE_TYPE_VALUES = {"制度", "流程", "指标", "常见问题", "案例", "概念", "总览"}
CHINA_TZ = timezone(timedelta(hours=8))
SKILL_ROOT = Path(__file__).resolve().parent.parent
ASSETS = SKILL_ROOT / "assets"
AI_HEADER_SCHEMA_VERSION = "kb-ai-document-v2"
AI_HEADER_FIELDS = (
    "schema_version", "stable_id", "view", "metadata", "distillation_profile",
)
DISTILLATION_PROFILE_FIELDS = (
    "core_theme", "business_objects", "document_role", "inputs", "actions", "outputs",
    "constraints",
)


def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def safe_name(value: str, limit: int = 90) -> str:
    value = re.sub(r"[\x00-\x1f/:]", "_", value).strip().rstrip(".") or "未命名"
    return value[:limit].rstrip()


def rel_link(from_path: Path, target: Path, label: str) -> str:
    relative = os.path.relpath(target, from_path.parent).replace(os.sep, "/")
    return f"[{label}]({quote(relative, safe='/._-')})"


def materialize_asset(name: str, replacements: dict[str, object]) -> str:
    path = ASSETS / name
    if not path.exists():
        raise RuntimeError(f"Skill缺少输出模板：{path}")
    text = path.read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace("{{" + key + "}}", str(value))
    unresolved = sorted(set(re.findall(r"\{\{[A-Z0-9_]+\}\}", text)))
    if unresolved:
        raise RuntimeError(f"输出模板仍有未替换占位符：{name}: {unresolved}")
    return text.rstrip() + "\n"


def compact_strings(values: object, limit: int = 8, item_limit: int = 180) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        text = re.sub(r"\s+", " ", str(value)).strip()
        if text:
            result.append(text[:item_limit])
        if len(result) >= limit:
            break
    return result


def unified_header_payload(profile: dict, metadata: dict, view_name: str) -> dict:
    content = profile.get("content_profile") or {}
    metadata_payload = {field: metadata[field] for field in STANDARD_METADATA_FIELDS}
    distillation_profile = {
        "core_theme": str(content.get("core_theme") or ""),
        "business_objects": list(content.get("business_objects") or []),
        "document_role": str(content.get("document_role") or ""),
        "inputs": list(content.get("inputs") or []),
        "actions": list(content.get("actions") or []),
        "outputs": list(content.get("outputs") or []),
        "constraints": list(content.get("constraints") or []),
    }
    payload = {
        "schema_version": AI_HEADER_SCHEMA_VERSION,
        "stable_id": str(profile.get("source_id") or ""),
        "view": view_name,
        "metadata": metadata_payload,
        "distillation_profile": distillation_profile,
    }
    if tuple(payload) != AI_HEADER_FIELDS:
        raise RuntimeError("统一AI检索元数据字段顺序异常")
    if tuple(metadata_payload) != STANDARD_METADATA_FIELDS:
        raise RuntimeError("17字段元数据顺序异常")
    if tuple(distillation_profile) != DISTILLATION_PROFILE_FIELDS:
        raise RuntimeError("蒸馏画像字段顺序异常")
    return payload


def unified_header_markdown(profile: dict, metadata: dict, view_name: str) -> str:
    payload = unified_header_payload(profile, metadata, view_name)
    lines = ["## AI检索元数据（17字段与蒸馏画像）", "", "```yaml"]
    for field in ("schema_version", "stable_id", "view"):
        lines.append(f"{field}: {json.dumps(payload[field], ensure_ascii=False)}")
    lines.append("metadata:")
    for field in STANDARD_METADATA_FIELDS:
        lines.append(f"  {field}: {json.dumps(payload['metadata'][field], ensure_ascii=False)}")
    lines.append("distillation_profile:")
    for field in DISTILLATION_PROFILE_FIELDS:
        lines.append(f"  {field}: {json.dumps(payload['distillation_profile'][field], ensure_ascii=False)}")
    lines.extend([
        "```", "",
        "> AI先读取本区完成候选筛选；需要回答细节或核实结论时，再读取相关知识和原文。", "",
    ])
    return "\n".join(lines)


def inline_text(value: object, limit: int = 180) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().replace("|", "｜")[:limit]


def write_directory_indexes(
    root: Path,
    page_path_by_id: dict[str, Path],
    metadata_by_id: dict[str, dict],
    view_label: str,
) -> dict[Path, Path]:
    id_by_path = {path.resolve(): source_id for source_id, path in page_path_by_id.items()}
    directories = {root}
    for path in page_path_by_id.values():
        current = path.parent
        while True:
            if current != root and root not in current.parents:
                raise RuntimeError(f"知识页超出视图根目录：{path}")
            directories.add(current)
            if current == root:
                break
            current = current.parent
    indexes: dict[Path, Path] = {}
    for directory in sorted(directories, key=lambda value: (len(value.parts), value.as_posix()), reverse=True):
        index_path = directory / "目录索引.md"
        indexes[directory] = index_path
        child_directories = sorted(
            child for child in directories
            if child.parent == directory and child != directory
        )
        current_pages = sorted(
            (path for path in page_path_by_id.values() if path.parent == directory),
            key=lambda path: path.name,
        )
        title = root.name if directory == root else directory.name
        lines = [
            f"# {title}｜目录索引", "",
            f"> {view_label}；当前层级包含 {len(child_directories)} 个子目录、{len(current_pages)} 份文档。", "",
        ]
        if child_directories:
            lines.extend(["## 子目录", ""])
            for child in child_directories:
                lines.append(f"- {rel_link(index_path, child / '目录索引.md', child.name)}")
            lines.append("")
        lines.extend(["## 当前文件夹文档", ""])
        if not current_pages:
            lines.extend(["- 暂无直接文档，请进入子目录。", ""])
        for page in current_pages:
            source_id = id_by_path[page.resolve()]
            metadata = metadata_by_id[source_id]
            scenario = "、".join(compact_strings(metadata.get("scenario"), 2, 60)) or "未标注"
            lines.extend([
                f"### {rel_link(index_path, page, str(metadata.get('title') or source_id))}", "",
                f"- 稳定ID：`{source_id}`",
                f"- 类型/状态：{metadata.get('page_type') or '未识别'} / {metadata.get('status') or '未标注'}",
                f"- 使用场景：{scenario}",
                f"- 一句话说明：{inline_text(metadata.get('summary'), 220) or '未生成摘要'}", "",
            ])
        index_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return indexes


def standard_page_type(candidate: str) -> str:
    if candidate in PAGE_TYPE_VALUES:
        return candidate
    mapping = {
        "规范": "制度",
        "标准": "指标",
        "数据": "指标",
        "会议纪要": "案例",
        "记录": "案例",
        "项目": "案例",
        "决策": "案例",
        "培训": "概念",
        "方案": "概念",
        "模板": "概念",
        "附件": "概念",
    }
    return mapping.get(candidate, "概念")


def format_datetime(value: object) -> str:
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            number = float(value)
            if number > 10_000_000_000:
                number /= 1000
            return datetime.fromtimestamp(number, tz=CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=CHINA_TZ)
        return parsed.astimezone(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError):
        return str(value)


def page_relations(source_id: str, ready: list[dict]) -> list[dict]:
    values: list[dict] = []
    for relation in ready:
        if relation.get("source_id") == source_id:
            related_object = relation.get("target_id", "")
        elif relation.get("target_id") == source_id:
            related_object = relation.get("source_id", "")
        else:
            continue
        values.append({
            "relation_type": relation.get("final_relation_type") or relation.get("verified_relation_type") or relation.get("proposed_type") or "关联",
            "relation_meaning": relation.get("relation_meaning", ""),
            "related_object": related_object,
        })
    return values


def metadata_updated_at(profile: dict, ready: list[dict], acceptance: object = None) -> str:
    source_id = str(profile.get("source_id") or "")
    values = [profile.get("generated_at"), profile.get("update_time")]
    for relation in ready:
        if source_id not in {relation.get("source_id"), relation.get("target_id")}:
            continue
        values.extend((relation.get("verified_at"), relation.get("business_confirmed_at")))
    accepted_hashes = common.accepted_document_hashes(acceptance)
    if accepted_hashes.get(source_id) == str(profile.get("source_hash") or "") and isinstance(acceptance, dict):
        values.append(acceptance.get("confirmed_at"))
    formatted = [format_datetime(value) for value in values if value not in (None, "")]
    return max(formatted) if formatted else ""


def build_standard_metadata(profile: dict, ready: list[dict], acceptance: object = None) -> dict:
    content = profile.get("content_profile") or {}
    source_url = common.sanitize_transient_url(profile.get("source_url"), profile.get("parent_node_id"))
    source_hash = str(profile.get("source_hash") or "")
    blindspots: list[str] = []
    if not profile.get("creator_name"):
        blindspots.append("来源元数据未返回原文创建者")
    if not profile.get("permission_snapshot"):
        blindspots.append("本地只读盘点未取得权限快照")
    return {
        "title": str(profile.get("file_name") or profile.get("source_id") or "未命名"),
        "page_type": standard_page_type(str(content.get("page_type_candidate") or "")),
        "scenario": list(content.get("scenarios") or []),
        "keywords": list(content.get("keywords") or [])[:6],
        "summary": str(content.get("summary") or ""),
        "owner": str(profile.get("creator_name") or profile.get("owner") or "待补充"),
        "status": common.document_business_status(profile, acceptance),
        "processing": "已蒸馏",
        "version": "1.1",
        "source": source_url,
        "source_updated_at": format_datetime(profile.get("update_time")),
        "content_hash": f"sha256:{source_hash}" if source_hash else "",
        "sources": [source_url] if source_url else [],
        "blindspots": blindspots,
        "relations": page_relations(str(profile.get("source_id") or ""), ready),
        "property_generated_at": format_datetime(profile.get("generated_at")),
        "property_updated_at": metadata_updated_at(profile, ready, acceptance),
    }


def relation_groups(source_id: str, relations: list[dict], by_id: dict[str, dict]) -> str:
    grouped: defaultdict[str, list[str]] = defaultdict(list)
    for relation in relations:
        if relation.get("source_id") == source_id:
            target = relation.get("target_id", "")
        elif relation.get("target_id") == source_id:
            target = relation.get("source_id", "")
        else:
            continue
        relation_type = relation.get("final_relation_type") or relation.get("verified_relation_type") or relation.get("proposed_type") or "关联"
        title = (by_id.get(target) or {}).get("file_name") or target
        grouped[relation_type].append(f"{title}（{target}）")
    if not grouped:
        return "- 暂无"
    return "\n".join(f"- **{kind}**：" + "、".join(dict.fromkeys(targets)) for kind, targets in sorted(grouped.items()))


def load_jsonish(value: object) -> object:
    if isinstance(value, (list, dict)):
        return value
    if value in (None, ""):
        return None
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return str(value)


def relation_evidence_lines(relation: dict) -> list[str]:
    lines: list[str] = []
    source_evidence = load_jsonish(relation.get("source_evidence"))
    target_evidence = load_jsonish(relation.get("target_evidence"))
    codex_evidence = load_jsonish(relation.get("codex_evidence"))
    if isinstance(source_evidence, list):
        lines.extend(f"- 来源文档证据：{value}" for value in source_evidence)
    if isinstance(target_evidence, list):
        lines.extend(f"- 目标文档证据：{value}" for value in target_evidence)
    if isinstance(codex_evidence, dict):
        for value in codex_evidence.get("source") or []:
            lines.append(f"- 来源文档证据：{value}")
        for value in codex_evidence.get("target") or []:
            lines.append(f"- 目标文档证据：{value}")
        if codex_evidence.get("reason"):
            lines.append(f"- Codex 核验理由：{codex_evidence['reason']}")
    elif isinstance(codex_evidence, str):
        lines.append(f"- Codex 核验证据：{codex_evidence}")
    if relation.get("verification_reason"):
        lines.append(f"- Codex 核验理由：{relation['verification_reason']}")
    if not lines:
        local_evidence = load_jsonish(relation.get("local_evidence"))
        if isinstance(local_evidence, dict):
            for rule_id, values in local_evidence.items():
                for value in values if isinstance(values, list) else [values]:
                    lines.append(f"- {rule_id} 本地证据：{value}")
        elif local_evidence:
            lines.append(f"- 本地证据：{local_evidence}")
    return lines


def relation_details_markdown(
    source_id: str,
    relations: list[dict],
    profiles: dict[str, dict],
    preview_path_by_id: dict[str, Path],
    current_page: Path,
    status_kind: str,
) -> str:
    relevant = [row for row in relations if source_id in {row.get("source_id"), row.get("target_id")}]
    if not relevant:
        return "- 暂无"
    lines: list[str] = []
    for index, relation in enumerate(sorted(relevant, key=lambda row: str(row.get("relation_id") or "")), start=1):
        current_is_source = relation.get("source_id") == source_id
        related_id = relation.get("target_id", "") if current_is_source else relation.get("source_id", "")
        related_title = relation.get("target_title", "") if current_is_source else relation.get("source_title", "")
        related_url = common.sanitize_transient_url(
            relation.get("target_url", "") if current_is_source else relation.get("source_url", "")
        )
        relation_type = relation.get("final_relation_type") or relation.get("verified_relation_type") or relation.get("proposed_type") or "关联"
        type_explanation = relation.get("relation_type_explanation") or common.relation_type_explanation(str(relation_type))
        raw_direction = str(relation.get("direction") or "未标明")
        if raw_direction == "来源→目标":
            direction = "本文 → 关联文档" if current_is_source else "关联文档 → 本文"
        elif raw_direction == "目标→来源":
            direction = "关联文档 → 本文" if current_is_source else "本文 → 关联文档"
        else:
            direction = raw_direction
        related_profile = profiles.get(str(related_id)) or {}
        related_role = (related_profile.get("content_profile") or {}).get("document_role") or "未识别"
        if related_id in preview_path_by_id:
            related_link = rel_link(current_page, preview_path_by_id[str(related_id)], str(related_title or related_id))
        else:
            related_link = str(related_title or related_id)
        review_level = relation.get("review_level") or "未分级"
        review_status = relation.get("business_review_status") or relation.get("review_status") or "未记录"
        if status_kind == "ready":
            review_status = f"已通过发布门槛（{review_status}），但本轮仍未线上发布"
        risk = relation.get("risk_level") or ", ".join(load_jsonish(relation.get("risk_flags")) or []) or "无显式风险"
        rules = load_jsonish(relation.get("rule_ids"))
        rule_text = "、".join(str(value) for value in rules) if isinstance(rules, list) else str(rules or "未记录")
        lines.extend([
            f"### {index}. 关联文档：{related_title or related_id}", "",
            f"- **关系 ID**：`{relation.get('relation_id') or '未记录'}`",
            f"- **关联文档**：{related_link}（稳定 ID：`{related_id}`）",
            f"- **关系方向**：{direction}（台账方向：{raw_direction}）",
            f"- **系统关系标签**：{relation_type}",
            f"- **标签通俗解释**：{type_explanation}",
            f"- **本条关系具体含义**：{relation.get('relation_meaning') or '未记录'}",
            f"- **对方文档角色**：{related_role}",
            f"- **审核状态**：{review_level}｜{review_status}｜风险：{risk}",
            f"- **触发规则**：{rule_text}",
        ])
        if status_kind != "ready":
            lines.extend([
                f"- **为什么列为 L3**：{relation.get('l3_reason') or relation.get('verification_reason') or '详见双方证据'}",
                f"- **负责人需要确认**：{relation.get('confirmation_question') or '请明确最终以哪份文档为准，并说明另一份文档如何处理。'}",
            ])
        if related_url:
            lines.append(f"- **钉钉原文**：[打开关联文档原文]({related_url})")
        evidence = relation_evidence_lines(relation)
        lines.extend(["", "**核验证据**", "", *(evidence or ["- 暂无可展示证据"]), ""])
    return "\n".join(lines)


def related_knowledge_markdown(
    source_id: str,
    relations: list[dict],
    target_page_by_id: dict[str, Path],
    current_page: Path,
    *,
    pending: bool = False,
) -> str:
    relevant = [row for row in relations if source_id in {row.get("source_id"), row.get("target_id")}]
    if not relevant:
        return "- 暂无"
    lines: list[str] = []
    for index, relation in enumerate(sorted(relevant, key=lambda row: str(row.get("relation_id") or "")), start=1):
        current_is_source = relation.get("source_id") == source_id
        related_id = str(relation.get("target_id", "") if current_is_source else relation.get("source_id", ""))
        related_title = str(relation.get("target_title", "") if current_is_source else relation.get("source_title", ""))
        related_url = common.sanitize_transient_url(
            relation.get("target_url", "") if current_is_source else relation.get("source_url", "")
        )
        relation_type = relation.get("final_relation_type") or relation.get("verified_relation_type") or relation.get("proposed_type") or "关联"
        relation_name = relation.get("business_relation_name") or common.business_relation_name(str(relation_type))
        if related_id in target_page_by_id:
            page_link = rel_link(current_page, target_page_by_id[related_id], related_title or related_id)
        else:
            page_link = related_title or related_id
        lines.extend([
            f"### {index}. {related_title or related_id}", "",
            f"- **关联文档名称**：{page_link}",
            f"- **两份文档存在什么关系**：{relation_name}",
            f"- **这段关系是什么意思**：{relation.get('relation_meaning') or '未记录'}",
        ])
        if related_url:
            lines.append(f"- **可点击的关联文档链接**：[打开钉钉原文]({related_url})")
        if pending:
            lines.append(f"- **负责人需要确认什么**：{relation.get('confirmation_question') or '请明确最终有效口径。'}")
        lines.append("")
    return "\n".join(lines).rstrip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the local review package before any optional publication.")
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()
    paths = common.job_paths(args.job)
    config = common.load_task_config(args.job)
    knowledge_base_name = str(config.get("source.workspace_name", "workspace_name", default="部门知识库") or "部门知识库")
    preview = args.job / "本地审核结果（仅供审核）"
    if preview.exists():
        shutil.rmtree(preview)
    raw_root = preview / "01-原文镜像（按原目录）"
    business_root = preview / "02-蒸馏结果（按文档类型）"
    relation_root = preview / "03-关系审核（L3确认与L2抽样）"
    exception_root = preview / "04-失败与异常（需补充处理）"
    metadata_root = preview / "05-元数据（17字段）"
    raw_root.mkdir(parents=True); business_root.mkdir(parents=True)
    relation_root.mkdir(parents=True); exception_root.mkdir(parents=True); metadata_root.mkdir(parents=True)
    manifest = common.load_manifest(args.job)
    manifest_by_id = {row["source_id"]: row for row in manifest}
    active_success_ids = {
        str(row.get("source_id") or "")
        for row in manifest
        if row.get("parse_status") in common.SUCCESS_STATUSES
    }
    profiles: dict[str, dict] = {}
    for path in paths["source_profiles"].glob("*.json"):
        value = common.load_json(path)
        if isinstance(value, dict) and value.get("source_id") in active_success_ids:
            profiles[value["source_id"]] = value
    ready = load_csv(paths["ledgers"] / "relation-publication-ready.csv")
    review = load_csv(paths["ledgers"] / "relation-review-queue.csv")
    sampling = load_csv(paths["ledgers"] / "relation-sampling-queue.csv")
    verification = load_csv(paths["ledgers"] / "relation-verification.csv")
    acceptance = common.load_json(paths["ledgers"] / "distillation-acceptance.json", {})
    preview_path_by_id: dict[str, Path] = {}
    raw_path_by_id: dict[str, Path] = {}
    type_counts: Counter = Counter()
    type_pages: defaultdict[str, list[tuple[str, str, Path]]] = defaultdict(list)
    metadata_by_id = {
        source_id: build_standard_metadata(profile, ready, acceptance)
        for source_id, profile in profiles.items()
    }
    preview_updated_at = max(
        (str(metadata.get("property_updated_at") or "") for metadata in metadata_by_id.values()),
        default="",
    )

    for source_id, profile in sorted(profiles.items(), key=lambda item: str(item[1].get("source_path") or "")):
        content = profile.get("content_profile") or {}
        type_name = safe_name(str(content.get("page_type_candidate") or "未识别"), 30)
        type_counts[type_name] += 1
        page = business_root / type_name / f"{safe_name(str(profile.get('file_name') or source_id))}（稳定ID：{source_id}）.md"
        raw_page = raw_root.joinpath(*(safe_name(part) for part in str(profile.get("source_path") or source_id).split("/")[:-1]), f"{safe_name(str(profile.get('file_name') or source_id))}（稳定ID：{source_id}）.md")
        page.parent.mkdir(parents=True, exist_ok=True); raw_page.parent.mkdir(parents=True, exist_ok=True)
        preview_path_by_id[source_id] = page; raw_path_by_id[source_id] = raw_page
        type_pages[type_name].append((str(profile.get("file_name") or source_id), source_id, page))

    for source_id, profile in profiles.items():
        page = preview_path_by_id[source_id]
        raw_page = raw_path_by_id[source_id]
        source_ready = [relation for relation in ready if source_id in {relation.get("source_id"), relation.get("target_id")}]
        source_pending = [relation for relation in review if source_id in {relation.get("source_id"), relation.get("target_id")}]
        extracted = paths["extracted"] / f"{source_id}.txt"
        original_text = common.sanitize_transient_urls(
            extracted.read_text(encoding="utf-8", errors="replace") if extracted.exists() else "未生成文本抽取结果。"
        )
        original_matches = sorted((args.job / "02-extraction-cache" / "originals").glob(f"{source_id}.*"))

        def render_full_page(current_page: Path, counterpart: Path, target_pages: dict[str, Path], view_name: str) -> None:
            source_url = common.sanitize_transient_url(profile.get("source_url"), profile.get("parent_node_id"))
            if view_name == "原目录视图":
                raw_links = [
                    "- 当前页面即按来源原目录层级生成的完整蒸馏知识页。",
                    f"- {rel_link(current_page, counterpart, '打开业务分类目录中的对应知识页')}",
                ]
                if source_url:
                    raw_links.append(f"- [打开完整原文镜像（钉钉来源）]({source_url})")
            else:
                raw_links = [
                    f"- {rel_link(current_page, counterpart, '打开完整原文镜像（按原目录）')}",
                ]
                if source_url:
                    raw_links.append(f"- [打开钉钉来源原文]({source_url})")
            lines = [
                f"# {profile.get('file_name') or source_id}", "",
                unified_header_markdown(profile, metadata_by_id[source_id], view_name),
                f"> 本页为知识库蒸馏审核预览（{view_name}）；只有任务配置提供最终写回目录后才会发布。", "",
                "## Raw Mirror", "",
                *raw_links, "",
                "## 相关知识", "",
                related_knowledge_markdown(source_id, source_ready, target_pages, current_page), "",
                "## 待业务确认的相关知识（确认前不会发布）", "",
                related_knowledge_markdown(source_id, source_pending, target_pages, current_page, pending=True), "",
                "## Original Content", "",
                original_text.rstrip(), "",
            ]
            if original_matches:
                lines.extend([
                    "## Original Attachment", "",
                    f"- {rel_link(current_page, original_matches[0], f'打开本地原件：{original_matches[0].name}')}", "",
                ])
            current_page.write_text("\n".join(lines), encoding="utf-8")

        render_full_page(raw_page, page, raw_path_by_id, "原目录视图")
        render_full_page(page, raw_page, preview_path_by_id, "业务分类视图")

    metadata_csv_rows: list[dict] = []
    for source_id, metadata in sorted(metadata_by_id.items()):
        metadata_csv_rows.append({
            field: json.dumps(metadata[field], ensure_ascii=False) if isinstance(metadata[field], (list, dict)) else metadata[field]
            for field in STANDARD_METADATA_FIELDS
        })
    common.write_csv(metadata_root / "17字段元数据总表.csv", metadata_csv_rows, list(STANDARD_METADATA_FIELDS))
    metadata_guide = """# 17字段元数据说明

每份已蒸馏文档页首只有一个 `AI检索元数据（17字段与蒸馏画像）` YAML 区块。
它把文档管理所需的 17 字段和检索所需的语义画像合并展示，每项信息只保存一次。

其中 `metadata` 固定使用以下 17 个键：

```text
title, page_type, scenario, keywords, summary, owner, status,
processing, version, source, source_updated_at, content_hash,
sources, blindspots, relations, property_generated_at, property_updated_at
```

- `page_type` 已归一到公司标准枚举：制度、流程、指标、常见问题、案例、概念、总览。
- `status` 初始为“候选”；本次任务执行审核人在AI对话框明确确认整批蒸馏无误后，内容哈希未变化的成功文档改为“正式”。
- `owner` 固定取钉钉原文创建者姓名。目录接口缺失时自动用文档搜索取得创建者UID，再通过通讯录取得姓名；仍不可得时写“待补充”，不得从正文或目录猜测。
- `relations` 只包含已通过发布门槛的关系；L2 核验通过后自动进入，L3 必须人工确认后才进入。
- `distillation_profile` 固定包含核心主题、业务对象、文档角色、输入、动作、产出、约束 7 项。
- 本目录中的 CSV 只用于批量审核与归档，不会在知识页里重复生成另一套元数据。

本文件是发布前审核产物；是否写回钉钉由任务启动配置中的 `publishing` 决定。
"""
    (metadata_root / "17字段元数据说明.md").write_text(metadata_guide, encoding="utf-8")

    # 两套视图的每一层文件夹都生成可打开的轻量索引。
    raw_indexes = write_directory_indexes(raw_root, raw_path_by_id, metadata_by_id, "原目录视图")
    business_indexes = write_directory_indexes(business_root, preview_path_by_id, metadata_by_id, "业务分类视图")

    query_entry = preview / "00-AI问答与检索入口.md"
    knowledge_map = preview / "00-AI知识库地图.md"
    raw_index = raw_indexes[raw_root]
    business_index = business_indexes[business_root]
    category_rows = []
    for kind, count in sorted(type_counts.items()):
        category_index = business_indexes[business_root / kind]
        category_rows.append(f"- {rel_link(knowledge_map, category_index, kind)}：{count} 份")
    query_entry.write_text(materialize_asset("ai-query-entry.md", {
        "KNOWLEDGE_BASE_NAME": knowledge_base_name,
        "GENERATED_AT": preview_updated_at,
        "DOCUMENT_COUNT": len(profiles),
        "KNOWLEDGE_MAP_LINK": rel_link(query_entry, knowledge_map, "AI知识库地图").split("(", 1)[1][:-1],
        "BUSINESS_INDEX_LINK": rel_link(query_entry, business_index, "业务分类索引").split("(", 1)[1][:-1],
        "RAW_INDEX_LINK": rel_link(query_entry, raw_index, "原目录索引").split("(", 1)[1][:-1],
    }), encoding="utf-8")
    knowledge_map.write_text(materialize_asset("ai-knowledge-map.md", {
        "KNOWLEDGE_BASE_NAME": knowledge_base_name,
        "GENERATED_AT": preview_updated_at,
        "DOCUMENT_COUNT": len(profiles),
        "READY_RELATION_COUNT": len(ready),
        "PENDING_RELATION_COUNT": len(review),
        "BUSINESS_INDEX_LINK": rel_link(knowledge_map, business_index, "业务分类索引").split("(", 1)[1][:-1],
        "RAW_INDEX_LINK": rel_link(knowledge_map, raw_index, "原目录索引").split("(", 1)[1][:-1],
        "CATEGORY_ROWS": "\n".join(category_rows) or "- 暂无可用类别",
    }), encoding="utf-8")

    glossary_source = Path(__file__).resolve().parent.parent / "references" / "10-名词解释与规则对照.md"
    glossary_target = preview / "名词解释与规则对照表.md"
    if glossary_source.exists():
        shutil.copy2(glossary_source, glossary_target)

    index_lines = [
        f"# {knowledge_base_name}｜知识库蒸馏审核入口", "",
        "> 这是发布前本地审核包；页面只有通过验收，且任务启动时已提供最终写回目录，才会写入钉钉。", "",
        "## 开始审核前先看", "",
        "- [AI问答与检索入口](00-AI%E9%97%AE%E7%AD%94%E4%B8%8E%E6%A3%80%E7%B4%A2%E5%85%A5%E5%8F%A3.md)：说明 Claude/Codex 如何低成本查询这个知识库。",
        "- [AI知识库地图](00-AI%E7%9F%A5%E8%AF%86%E5%BA%93%E5%9C%B0%E5%9B%BE.md)：用于选择业务范围和目录浏览。",
        "- [名词解释与规则对照表](名词解释与规则对照表.md)：解释稳定 ID、17 字段、R01-R10、L0-L3、候选关系、ACL、发布门槛等术语。",
        f"- [17字段元数据说明]({quote('05-元数据（17字段）')}/{quote('17字段元数据说明.md')})：说明每份蒸馏文档页首的固定字段。", "",
        "## 处理概况", "",
        f"- 来源文件：{len(manifest)}",
        f"- 已生成语义画像：{len(profiles)}",
        f"- Codex 已核验关系：{len(verification)}",
        f"- 已通过发布门槛关系：{len(ready)}",
        f"- L2 自动通过关系：{sum(row.get('review_level') == 'L2' for row in ready)}（本次抽样 {len(sampling)} 条）",
        f"- L3 待业务确认关系：{len(review)}", "",
        "## 按文档类型查看", "",
    ]
    for kind, count in sorted(type_counts.items()):
        type_index = business_indexes[business_root / kind]
        index_lines.append(f"- {rel_link(preview / '审核入口.md', type_index, kind)}：{count} 份")
    relation_folder = quote("03-关系审核（L3确认与L2抽样）")
    index_lines.extend(["", "## 审核清单", "", f"- [17字段元数据总表]({quote('05-元数据（17字段）')}/{quote('17字段元数据总表.csv')})", f"- [L3高风险关系确认清单]({relation_folder}/{quote('L3高风险关系确认清单.md')})", f"- [L2自动通过关系抽样清单]({relation_folder}/{quote('L2自动通过关系抽样清单.md')})", f"- [关系总览]({relation_folder}/{quote('关系总览.md')})", f"- [经Raw标准排除原因汇总]({quote('04-失败与异常（需补充处理）')}/{quote('经Raw标准排除原因汇总.md')})", f"- [经Raw标准排除清单]({quote('04-失败与异常（需补充处理）')}/{quote('经Raw标准排除清单.csv')})", f"- [解析失败原因汇总]({quote('04-失败与异常（需补充处理）')}/{quote('解析失败原因汇总.md')})", f"- [解析失败清单]({quote('04-失败与异常（需补充处理）')}/{quote('解析失败清单.csv')})", ""])
    (preview / "审核入口.md").write_text("\n".join(index_lines), encoding="utf-8")

    review_lines = [
        "# L3 高风险关系确认清单（本地）", "",
        "> 本清单只保留负责人确认所需信息；技术证据和规则编号保存在后台台账，不在这里展示。所有钉钉链接仅供查看，不会产生回写。", "",
    ]
    for index, row in enumerate(review, start=1):
        source_url = common.sanitize_transient_url(row.get("source_url"))
        target_url = common.sanitize_transient_url(row.get("target_url"))
        source_link = f"[打开原文]({source_url})" if source_url else "未取得"
        target_link = f"[打开原文]({target_url})" if target_url else "未取得"
        review_lines.extend([
            f"## {index}. {row.get('source_title','')} ↔ {row.get('target_title','')}", "",
            "| 文档 | 创建者 | 原文链接 |", "|---|---|---|",
            f"| {row.get('source_title','')} | {row.get('source_creator_name') or '钉钉未返回创建者（待补充）'} | {source_link} |",
            f"| {row.get('target_title','')} | {row.get('target_creator_name') or '钉钉未返回创建者（待补充）'} | {target_link} |", "",
            f"- **存在什么关系**：{row.get('business_relation_name') or common.business_relation_name(row.get('proposed_type',''))}",
            f"- **这段关系是什么意思**：{row.get('relation_meaning','')}",
            f"- **负责人需要确认什么**：{row.get('confirmation_question') or '请明确最终以哪份文档为准，并说明另一份文档如何处理。'}", "",
        ])
    (relation_root / "L3高风险关系确认清单.md").write_text("\n".join(review_lines) + "\n", encoding="utf-8")

    sampling_lines = [
        "# L2 自动通过关系抽样清单（本地）", "",
        f"> L2 是证据充分的普通业务关联，核验通过后自动进入 `relations`；本次从自动通过关系中抽取 {len(sampling)} 条事后检查，不阻断发布门槛。", "",
        "如抽查发现关系类型、方向或解释错误，请在《L2关系抽样明细.csv》中把 `sample_status` 改为“抽查需修正”并写明原因，该关系随后撤回并升级复审。", "",
        "| 抽查状态 | 来源 | 系统关系标签 | 标签通俗解释 | 目标 | 方向 | 本条关系具体含义 |", "|---|---|---|---|---|---|---|",
    ]
    sampling_page = relation_root / "L2自动通过关系抽样清单.md"
    for row in sampling:
        source = rel_link(sampling_page, preview_path_by_id[row["source_id"]], row["source_title"]) if row.get("source_id") in preview_path_by_id else row.get("source_title", "")
        target = rel_link(sampling_page, preview_path_by_id[row["target_id"]], row["target_title"]) if row.get("target_id") in preview_path_by_id else row.get("target_title", "")
        if row.get("source_url"):
            source += f"<br>[钉钉原文]({common.sanitize_transient_url(row['source_url'])})"
        if row.get("target_url"):
            target += f"<br>[钉钉原文]({common.sanitize_transient_url(row['target_url'])})"
        type_explanation = str(row.get("relation_type_explanation") or common.relation_type_explanation(row.get("verified_relation_type", ""))).replace("|", "｜").replace("\n", " ")
        sampling_lines.append(f"| {row.get('sample_status','')} | {source} | {row.get('verified_relation_type','')} | {type_explanation} | {target} | {row.get('direction','')} | {str(row.get('relation_meaning','')).replace('|','｜')} |")
    sampling_page.write_text("\n".join(sampling_lines) + "\n", encoding="utf-8")

    relation_overview_page = relation_root / "关系总览.md"
    relation_lines = [
        "# 关系总览（本地）", "",
        f"- 已核验：{len(verification)}", f"- 已通过发布门槛：{len(ready)}", f"- L2 抽样：{len(sampling)}", f"- L3 待确认：{len(review)}", "",
        "## 已通过发布门槛的关系", "",
    ]
    for row in ready:
        relation_lines.extend([
            f"## {row.get('source_title')}（{row.get('source_id')}）", "",
            relation_details_markdown(str(row.get("source_id") or ""), [row], profiles, preview_path_by_id, relation_overview_page, "ready"), "",
        ])
    relation_lines.extend(["## L3 待业务确认的关系", ""])
    for row in review:
        relation_lines.extend([
            f"## {row.get('source_title')}（{row.get('source_id')}）", "",
            relation_details_markdown(str(row.get("source_id") or ""), [row], profiles, preview_path_by_id, relation_overview_page, "pending"), "",
        ])
    relation_overview_page.write_text("\n".join(relation_lines), encoding="utf-8")
    queue_csv = paths["ledgers"] / "relation-review-queue.csv"
    if queue_csv.exists():
        shutil.copy2(queue_csv, relation_root / "L3后台审核台账（技术人员使用）.csv")
    sampling_csv = paths["ledgers"] / "relation-sampling-queue.csv"
    if sampling_csv.exists():
        shutil.copy2(sampling_csv, relation_root / "L2关系抽样明细.csv")
    admission_csv = args.job / "01-inventory" / "raw-admission" / "经Raw标准排除清单.csv"
    if admission_csv.exists():
        excluded_rows = load_csv(admission_csv)
        safe_excluded_rows = [
            {
                key: (
                    common.sanitize_transient_url(value, row.get("parent_node_id"))
                    if key == "source_url" else common.sanitize_transient_urls(value)
                )
                for key, value in row.items()
            }
            for row in excluded_rows
        ]
        if safe_excluded_rows:
            common.write_csv(exception_root / "经Raw标准排除清单.csv", safe_excluded_rows, list(safe_excluded_rows[0]))
        excluded_counts = Counter((row.get("admission_reason", "未说明"), row.get("extension", "未知")) for row in excluded_rows)
        admission_lines = [
            "# 经Raw标准排除原因汇总", "",
            f"- 排除记录：{len(excluded_rows)}",
            "- 这些材料不进入解析、Codex和蒸馏成功/失败统计。", "",
            "| 排除原因 | 扩展名 | 数量 |", "|---|---|---:|",
        ]
        for (reason, extension), count in sorted(excluded_counts.items(), key=lambda item: (-item[1], item[0])):
            admission_lines.append(f"| {reason} | {extension} | {count} |")
        admission_lines.append("")
        (exception_root / "经Raw标准排除原因汇总.md").write_text("\n".join(admission_lines), encoding="utf-8")
    failure_csv = args.job / "01-inventory" / "parse-failure-list.csv"
    if failure_csv.exists():
        failures = load_csv(failure_csv)
        safe_failures = [
            {key: common.sanitize_transient_urls(value) for key, value in row.items()}
            for row in failures
        ]
        if safe_failures:
            common.write_csv(exception_root / "解析失败清单.csv", safe_failures, list(safe_failures[0]))
        failure_counts = Counter((row.get("failure_category", "未分类"), row.get("file_type", "未知")) for row in failures)
        failure_lines = [
            "# 解析失败原因汇总", "",
            f"- 失败记录：{len(failures)}",
            "- 说明：经Raw标准排除的格式不计入解析失败；详见Raw标准排除清单。", "",
            "| 失败类型 | 文件类型 | 数量 | 原因 | 建议处理 |", "|---|---|---:|---|---|",
        ]
        reason_map = {
            "下载失败": "未从只读下载链接取得有效附件",
            "DWS未登录": "DWS 认证已失效，无法只读导出在线表格",
            "动态HTML渲染失败": "页面依赖脚本渲染，静态文本不足且本地渲染超时",
        }
        action_map = {
            "下载失败": "恢复只读权限后按清单单独重试",
            "DWS未登录": "完成 dws auth login 后仅重试失败清单中的在线表格",
            "动态HTML渲染失败": "人工打开原文确认，或导出为 PDF/静态 HTML 后重试",
        }
        for (category, file_type), count in sorted(failure_counts.items(), key=lambda item: (-item[1], item[0])):
            failure_lines.append(f"| {category} | {file_type} | {count} | {reason_map.get(category, '详见失败明细')} | {action_map.get(category, '按明细逐项处理')} |")
        failure_lines.extend(["", "逐文件记录、来源路径、错误和建议操作见《解析失败清单.csv》。", ""])
        (exception_root / "解析失败原因汇总.md").write_text("\n".join(failure_lines), encoding="utf-8")
    directory_guide = """# 目录说明

- `00-AI问答与检索入口.md`：Claude/Codex 的固定检索路径。
- `00-AI知识库地图.md`：轻量业务导航，不是巨大的全量文档表。
- `00-蒸馏结果与下一步.md`：负责人先看的结果摘要和下一步。
- `审核入口.md`：先看总体数量和审核入口。
- `01-原文镜像（按原目录）`：按钉钉原目录层级组织的完整蒸馏知识页，包含页首统一AI检索元数据、Raw Mirror、相关知识和完整原文。
- `02-蒸馏结果（按文档类型）`：按业务类型组织的同一套完整蒸馏知识页，内容结构与原目录视图一致。
- `03-关系审核（L3确认与L2抽样）`：业务负责人只看精简确认清单；技术证据保留在后台台账。
- `04-失败与异常（需补充处理）`：Raw标准排除、下载、权限或解析失败明细。
- `05-元数据（17字段）`：公司标准 17 字段的说明与全量汇总表。
- `06-执行结果与下一步`：逐文档处理矩阵和待办动作清单。
- `名词解释与规则对照表.md`：解释蒸馏过程中使用的技术名词、R01-R10 规则与 L0-L3 分级。

本目录用于发布前本地审核；是否发布由任务启动时配置的最终写回目录决定。
"""
    (preview / "目录说明.md").write_text(directory_guide, encoding="utf-8")
    from build_delivery_summary import build as build_delivery_summary
    build_delivery_summary(args.job)
    print(f"LOCAL_PREVIEW_OK profiles={len(profiles)} ready_relations={len(ready)} l2_sample={len(sampling)} pending_l3={len(review)} path={preview}")


if __name__ == "__main__":
    main()
