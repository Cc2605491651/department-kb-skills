#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_common as common
from config_utils import is_local_only, publication_target


STANDARD_METADATA_FIELDS = [
    "title", "page_type", "scenario", "keywords", "summary", "owner", "status",
    "processing", "version", "source", "source_updated_at", "content_hash", "sources",
    "blindspots", "relations", "property_generated_at", "property_updated_at",
]
PAGE_TYPE_VALUES = {"制度", "流程", "指标", "常见问题", "案例", "概念", "总览"}
AI_HEADER_FIELDS = ["schema_version", "stable_id", "view", "metadata", "distillation_profile"]
DISTILLATION_PROFILE_FIELDS = [
    "core_theme", "business_objects", "document_role", "inputs", "actions", "outputs", "constraints",
]


def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def parse_unified_header(page_text: str) -> tuple[dict, list[str], list[str], list[str]]:
    match = re.search(
        r"\A#\s+[^\n]+\n\n## AI检索元数据（17字段与蒸馏画像）\s+```yaml\s+(.*?)\s+```",
        page_text,
        re.S,
    )
    if not match:
        return {}, [], [], []
    payload: dict[str, object] = {}
    top_order: list[str] = []
    metadata_order: list[str] = []
    profile_order: list[str] = []
    section = ""
    try:
        for line in match.group(1).splitlines():
            if line.startswith("  "):
                key, raw_value = line.strip().split(":", 1)
                if section not in {"metadata", "distillation_profile"}:
                    raise ValueError("nested field without section")
                nested = payload.setdefault(section, {})
                if not isinstance(nested, dict):
                    raise ValueError("invalid nested section")
                nested[key] = json.loads(raw_value.strip())
                (metadata_order if section == "metadata" else profile_order).append(key)
                continue
            key, raw_value = line.split(":", 1)
            top_order.append(key)
            if not raw_value.strip():
                section = key
                payload[key] = {}
            else:
                section = ""
                payload[key] = json.loads(raw_value.strip())
    except (ValueError, json.JSONDecodeError):
        return {}, top_order, metadata_order, profile_order
    return payload, top_order, metadata_order, profile_order


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate the local distillation package before optional publication.")
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()
    paths = common.job_paths(args.job)
    config = common.load_task_config(args.job)
    checks: list[tuple[str, bool, str]] = []
    target = publication_target(config)
    safe_boundary = is_local_only(config) or bool(target)
    dangerous = any(config.bool(f"publishing.{key}", default=False) for key in ("allow_delete", "allow_move", "allow_permission_change", "allow_overwrite_existing"))
    checks.append(("写入边界明确", safe_boundary and not dangerous, f"mode={'configured_publish_target' if target else 'local_only'} dangerous_permissions={dangerous}"))
    manifest = common.load_manifest(args.job)
    excluded_path = args.job / "01-inventory" / "raw-admission" / "excluded-manifest.json"
    excluded = common.load_json(excluded_path, [])
    if not isinstance(excluded, list):
        excluded = []
    rejected_extensions = {"bmp", "gif", "heic", "heif", "ico", "jpeg", "jpg", "png", "svg", "tif", "tiff", "webp", "aac", "flac", "m4a", "mp3", "ogg", "wav", "wma", "avi", "mkv", "mov", "mp4", "mpeg", "mpg", "webm", "wmv", "7z", "bz2", "gz", "rar", "tar", "tgz", "zip", "xz"}
    active_rejected = [row.get("source_id", "") for row in manifest if str(row.get("extension") or "").lower() in rejected_extensions or str(row.get("content_type") or "").upper() in {"AUDIO", "VIDEO", "ARCHIVE", "ARCHIVE_MEMBER"}]
    checks.append(("Raw拒绝格式未进入蒸馏", not active_rejected, f"active_rejected={len(active_rejected)} excluded={len(excluded)}"))
    admission_snapshot = common.load_json(args.job / "01-inventory" / "raw-admission" / "raw-manifest-before-admission.json", [])
    admission_closed = isinstance(admission_snapshot, list) and len(admission_snapshot) == len(manifest) + len(excluded)
    checks.append(("Raw准入总量闭合", admission_closed, f"inventory={len(admission_snapshot) if isinstance(admission_snapshot, list) else 'invalid'} accepted={len(manifest)} excluded={len(excluded)}"))
    successes = [row for row in manifest if row.get("parse_status") in common.SUCCESS_STATUSES]
    failures = [row for row in manifest if row.get("parse_status") == "失败已登记"]
    pending = [row for row in manifest if row.get("parse_status") == "待解析"]
    other = len(manifest) - len(successes) - len(failures) - len(pending)
    checks.append(("解析总量闭合", not pending and other == 0 and len(manifest) == len(successes) + len(failures), f"total={len(manifest)} success={len(successes)} failure={len(failures)} pending={len(pending)} other={other}"))
    missing_profiles: list[str] = []
    invalid_profiles: list[str] = []
    for row in successes:
        path = paths["source_profiles"] / f"{row['source_id']}.json"
        profile = common.load_json(path)
        if not isinstance(profile, dict):
            missing_profiles.append(row["source_id"]); continue
        content = profile.get("content_profile") or {}
        serialized = json.dumps(content, ensure_ascii=False)
        if not content.get("summary") or re.match(r"^(?:老大|老板|领导|用户|您好)[，,：:\s]", str(content.get("summary"))) or re.search(r"(?i)(?:密码|password|passcode)\s*[:：=]\s*(?!\[REDACTED\])[^\s,，;；。]{3,}", serialized):
            invalid_profiles.append(row["source_id"])
    checks.append(("Codex 来源画像齐全", not missing_profiles, f"missing={len(missing_profiles)}"))
    checks.append(("摘要无对话称呼和明文凭据", not invalid_profiles, f"invalid={len(invalid_profiles)}"))
    unresolved_owners = [
        row.get("source_id", "") for row in successes
        if not str(row.get("creator_name") or "").strip()
        or str(row.get("owner") or "").strip() in {"", "待补充"}
    ]
    checks.append(("Owner已取得钉钉原文创建者姓名", not unresolved_owners, f"unresolved={len(unresolved_owners)}"))
    candidates = load_csv(paths["ledgers"] / "relation-candidates.csv")
    invalid_gates = [row["relation_id"] for row in candidates if row.get("gate_status") == "eligible" and int(row.get("strong_count") or 0) < 1 and int(row.get("medium_count") or 0) < 2]
    checks.append(("R01-R10 候选门槛", not invalid_gates, f"invalid={len(invalid_gates)}"))
    verification = load_csv(paths["ledgers"] / "relation-verification.csv")
    ready = load_csv(paths["ledgers"] / "relation-publication-ready.csv")
    queue = load_csv(paths["ledgers"] / "relation-review-queue.csv")
    sampling = load_csv(paths["ledgers"] / "relation-sampling-queue.csv")
    ready_ids = {row.get("relation_id") for row in ready}
    queue_by_id = {row.get("relation_id"): row for row in queue}
    confirmed_l2_ids = {
        row.get("relation_id") for row in verification
        if row.get("review_level") == "L2" and row.get("verification_status") == "confirmed"
    }
    missing_l2_ready = sorted(confirmed_l2_ids - ready_ids)
    l2_in_manual_queue = sorted(
        row.get("relation_id") for row in queue
        if row.get("review_level") == "L2" and row.get("relation_id")
    )
    sample_ids = {row.get("relation_id") for row in sampling if row.get("relation_id")}
    invalid_sample = sorted(sample_ids - confirmed_l2_ids)
    sample_missing = bool(confirmed_l2_ids) and not sampling
    checks.append(("L2 自动通过并进入事后抽样", not missing_l2_ready and not l2_in_manual_queue and not invalid_sample and not sample_missing, f"l2={len(confirmed_l2_ids)} ready_missing={len(missing_l2_ready)} manual_queue={len(l2_in_manual_queue)} sample={len(sampling)} invalid_sample={len(invalid_sample)}"))
    confirmed_statuses = {"已确认", "修改后确认", "已发布", "已归档"}
    invalid_l3_ready = [
        row.get("relation_id", "") for row in ready
        if row.get("review_level") == "L3"
        and (queue_by_id.get(row.get("relation_id"), {}).get("review_status") not in confirmed_statuses)
    ]
    non_l3_manual = [row.get("relation_id", "") for row in queue if row.get("review_level") != "L3"]
    checks.append(("L3 高风险关系逐条确认", not invalid_l3_ready and not non_l3_manual, f"queue={len(queue)} invalid_ready={len(invalid_l3_ready)} non_l3={len(non_l3_manual)}"))
    preview = args.job / "本地审核结果（仅供审核）"
    preview_pages = list(preview.rglob("*.md")) if preview.exists() else []
    checks.append(("本地审核预览已生成", (preview / "审核入口.md").exists() and len(preview_pages) >= len(successes), f"preview_pages={len(preview_pages)} profiles={len(successes)}"))
    sensitive_url_files: list[str] = []
    signed_url_pattern = re.compile(
        r"https?://[^\s<>\"']*[?&](?:OSSAccessKeyId|AccessKeyId|Signature|Expires|X-Oss-Security-Token)=",
        re.I,
    )
    for path in (preview.rglob("*") if preview.exists() else []):
        if not path.is_file() or path.suffix.lower() not in {".md", ".csv", ".json", ".yaml", ".yml", ".txt"}:
            continue
        if signed_url_pattern.search(path.read_text(encoding="utf-8", errors="replace")):
            sensitive_url_files.append(path.relative_to(preview).as_posix())
    checks.append(("临时签名链接已清理", not sensitive_url_files, f"files={len(sensitive_url_files)}"))
    l3_page = preview / "03-关系审核（L3确认与L2抽样）" / "L3高风险关系确认清单.md"
    l3_text = l3_page.read_text(encoding="utf-8", errors="replace") if l3_page.exists() else ""
    incomplete_l3_explanations = [
        row.get("relation_id", "") for row in queue
        if not all(row.get(field) for field in ["source_title", "source_url", "source_creator_name", "target_title", "target_url", "target_creator_name", "business_relation_name", "relation_meaning", "confirmation_question"])
        or row.get("source_title", "") not in l3_text
        or row.get("target_title", "") not in l3_text
        or row.get("source_creator_name", "") not in l3_text
        or row.get("target_creator_name", "") not in l3_text
    ]
    required_l3_labels = ["文档", "创建者", "原文链接", "存在什么关系", "这段关系是什么意思", "负责人需要确认什么"]
    missing_l3_labels = [label for label in required_l3_labels if label not in l3_text]
    prohibited_l3_labels = ["关系 ID", "系统关系标签", "为什么列为 L3", "触发规则", "双方原文证据"]
    leaked_technical_labels = [label for label in prohibited_l3_labels if label in l3_text]
    checks.append(("L3 业务清单精简且信息完整", l3_page.exists() and not incomplete_l3_explanations and not missing_l3_labels and not leaked_technical_labels, f"relations={len(queue)} incomplete={len(incomplete_l3_explanations)} missing_labels={len(missing_l3_labels)} technical_labels={len(leaked_technical_labels)}"))
    metadata_csv = preview / "05-元数据（17字段）" / "17字段元数据总表.csv"
    batch_acceptance = common.load_json(paths["ledgers"] / "distillation-acceptance.json", {})
    metadata_rows: list[dict] = []
    metadata_header: list[str] = []
    if metadata_csv.exists():
        with metadata_csv.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            metadata_header = list(reader.fieldnames or [])
            metadata_rows = list(reader)
    invalid_metadata: list[str] = []
    for index, row in enumerate(metadata_rows, start=2):
        if (
            row.get("page_type") not in PAGE_TYPE_VALUES
            or row.get("status") not in {"候选", "正式", "已失效", "待确认"}
            or row.get("processing") not in {"已蒸馏", "平台字符限制-原附件保全"}
            or row.get("version") != "1.1"
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", row.get("content_hash", ""))
            or not row.get("title") or not row.get("summary") or not row.get("source")
        ):
            invalid_metadata.append(f"csv:{index}")
            continue
        try:
            for field in ["scenario", "keywords", "sources", "blindspots", "relations"]:
                value = json.loads(row.get(field, ""))
                if not isinstance(value, list):
                    raise ValueError(field)
        except (json.JSONDecodeError, ValueError):
            invalid_metadata.append(f"csv:{index}")
    expected_formal_count = sum(
        common.document_business_status(
            common.load_json(paths["source_profiles"] / f"{row.get('source_id', '')}.json", {}),
            batch_acceptance,
        ) == "正式"
        for row in successes
    )
    actual_formal_count = sum(row.get("status") == "正式" for row in metadata_rows)
    checks.append(("批次确认与文档状态一致", actual_formal_count == expected_formal_count, f"formal={actual_formal_count} expected={expected_formal_count}"))
    business_root = preview / "02-蒸馏结果（按文档类型）"
    raw_root = preview / "01-原文镜像（按原目录）"
    invalid_headers: list[str] = []
    for row in successes:
        business_matches = list(business_root.rglob(f"*（稳定ID：{row['source_id']}）.md")) if business_root.exists() else []
        raw_matches = list(raw_root.rglob(f"*（稳定ID：{row['source_id']}）.md")) if raw_root.exists() else []
        if len(business_matches) != 1 or len(raw_matches) != 1:
            invalid_headers.append(row["source_id"])
            continue
        business_text_page = business_matches[0].read_text(encoding="utf-8", errors="replace")
        raw_text_page = raw_matches[0].read_text(encoding="utf-8", errors="replace")
        required_sections = ["## Raw Mirror", "## 相关知识", "## Original Content"]
        header_payloads: list[dict] = []
        for page_text, expected_view in ((business_text_page, "业务分类视图"), (raw_text_page, "原目录视图")):
            payload, top_order, metadata_order, profile_order = parse_unified_header(page_text)
            if (
                top_order != AI_HEADER_FIELDS
                or metadata_order != STANDARD_METADATA_FIELDS
                or profile_order != DISTILLATION_PROFILE_FIELDS
                or payload.get("schema_version") != "kb-ai-document-v2"
                or payload.get("stable_id") != row["source_id"]
                or payload.get("view") != expected_view
                or any(section not in page_text for section in required_sections)
                or any(old_heading in page_text for old_heading in (
                    "## AI检索卡", "## 标准元数据（17字段）", "## 蒸馏画像",
                ))
            ):
                invalid_headers.append(f"{row['source_id']}:{expected_view}")
            header_payloads.append(payload)
        if len(header_payloads) == 2:
            left = {key: value for key, value in header_payloads[0].items() if key != "view"}
            right = {key: value for key, value in header_payloads[1].items() if key != "view"}
            if left != right:
                invalid_headers.append(f"{row['source_id']}:双视图不一致")
    metadata_ok = metadata_header == STANDARD_METADATA_FIELDS and len(metadata_rows) == len(successes) and not invalid_metadata
    checks.append(("17字段元数据完整", metadata_ok, f"rows={len(metadata_rows)} header_ok={metadata_header == STANDARD_METADATA_FIELDS} invalid={len(invalid_metadata)}"))
    checks.append(("统一AI检索元数据固定且双视图一致", not invalid_headers, f"pages={len(successes) * 2} invalid={len(invalid_headers)}"))

    required_root_files = [
        preview / "00-AI问答与检索入口.md",
        preview / "00-AI知识库地图.md",
        preview / "00-蒸馏结果与下一步.md",
        preview / "06-执行结果与下一步" / "文档处理结果矩阵.csv",
        preview / "06-执行结果与下一步" / "待办动作清单.md",
    ]
    missing_root_files = [path.name for path in required_root_files if not path.exists()]
    ai_entry_text = required_root_files[0].read_text(encoding="utf-8", errors="replace") if required_root_files[0].exists() else ""
    ai_map_text = required_root_files[1].read_text(encoding="utf-8", errors="replace") if required_root_files[1].exists() else ""
    root_content_ok = (
        "Agent固定路径" in ai_entry_text
        and "dws doc search" in ai_entry_text
        and "--start-index 0 --end-index 1" in ai_entry_text
        and "按业务类型查找" in ai_map_text
        and "AI全量索引" not in ai_map_text
    )
    checks.append(("AI问答入口、轻量地图与交付清单", not missing_root_files and root_content_ok, f"missing={missing_root_files} content_ok={root_content_ok}"))

    missing_directory_indexes: list[str] = []
    for root in (raw_root, business_root):
        if not root.exists():
            missing_directory_indexes.append(root.name)
            continue
        for directory in [root, *(path for path in root.rglob("*") if path.is_dir())]:
            if not (directory / "目录索引.md").exists():
                missing_directory_indexes.append(directory.relative_to(preview).as_posix())
    checks.append(("两套视图每层目录均有索引", not missing_directory_indexes, f"missing={len(missing_directory_indexes)}"))

    matrix_path = required_root_files[3]
    matrix_rows = load_csv(matrix_path)
    excluded_scope = load_csv(args.job / "01-inventory" / "excluded-scope" / "excluded-manifest.csv")
    expected_matrix_ids = {str(row.get("source_id") or "") for row in [*manifest, *excluded, *excluded_scope] if row.get("source_id")}
    matrix_ids = {str(row.get("稳定ID") or "") for row in matrix_rows if row.get("稳定ID")}
    expected_matrix_header = ["稳定ID", "文档标题", "创建者", "原文链接", "原路径", "文件类型", "Raw准入结果", "解析结果", "语义画像结果", "关联结果", "页面结果", "发布结果", "回读结果", "下一步动作"]
    matrix_header = list(matrix_rows[0].keys()) if matrix_rows else []
    checks.append(("文档处理结果矩阵总量闭合", matrix_header == expected_matrix_header and matrix_ids == expected_matrix_ids, f"rows={len(matrix_rows)} expected={len(expected_matrix_ids)} missing={len(expected_matrix_ids - matrix_ids)} extra={len(matrix_ids - expected_matrix_ids)}"))
    expected_related_documents = {
        document_id
        for relation in ready
        for document_id in (relation.get("source_id"), relation.get("target_id"))
        if document_id
    }
    metadata_with_relations = 0
    for row in metadata_rows:
        try:
            if json.loads(row.get("relations") or "[]"):
                metadata_with_relations += 1
        except json.JSONDecodeError:
            pass
    checks.append(("已通过关系写入文档元数据", metadata_with_relations == len(expected_related_documents), f"documents={metadata_with_relations} expected={len(expected_related_documents)}"))
    glossary = preview / "名词解释与规则对照表.md"
    glossary_text = glossary.read_text(encoding="utf-8", errors="replace") if glossary.exists() else ""
    glossary_terms = ["稳定 ID", "17 字段", "R01", "R10", "L0", "L3", "ACL", "发布门槛"]
    missing_terms = [term for term in glossary_terms if term not in glossary_text]
    checks.append(("名词解释与规则对照表", glossary.exists() and not missing_terms, f"missing_terms={len(missing_terms)}"))
    incomplete_relation_details: list[str] = []
    for relation in verification:
        relation_id = relation.get("relation_id", "")
        has_evidence = bool(relation.get("source_evidence") or relation.get("target_evidence") or relation.get("codex_evidence") or relation.get("local_evidence"))
        source_id = relation.get("source_id", "")
        target_id = relation.get("target_id", "")
        source_business = list(business_root.rglob(f"*（稳定ID：{source_id}）.md")) if source_id else []
        target_business = list(business_root.rglob(f"*（稳定ID：{target_id}）.md")) if target_id else []
        source_raw = list(raw_root.rglob(f"*（稳定ID：{source_id}）.md")) if source_id else []
        target_raw = list(raw_root.rglob(f"*（稳定ID：{target_id}）.md")) if target_id else []
        page_paths = source_business + target_business + source_raw + target_raw
        meaning = str(relation.get("relation_meaning") or "")
        opposite_titles = [
            str(relation.get("target_title") or ""),
            str(relation.get("source_title") or ""),
            str(relation.get("target_title") or ""),
            str(relation.get("source_title") or ""),
        ]
        page_details_complete = len(page_paths) == 4 and all(
            meaning in path.read_text(encoding="utf-8", errors="replace")
            and title in path.read_text(encoding="utf-8", errors="replace")
            for path, title in zip(page_paths, opposite_titles)
        )
        if (
            not relation_id or not relation.get("direction") or not relation.get("relation_meaning") or not has_evidence
            or not page_details_complete
        ):
            incomplete_relation_details.append(relation_id or "未知")
    checks.append(("两套目录的关联文档解释完整", not incomplete_relation_details, f"relations={len(verification)} incomplete={len(incomplete_relation_details)}"))
    invalid_raw_navigation: list[str] = []
    invalid_original_content: list[str] = []
    for row in successes:
        raw_matches = list(raw_root.rglob(f"*（稳定ID：{row['source_id']}）.md")) if raw_root.exists() else []
        business_matches = list(business_root.rglob(f"*（稳定ID：{row['source_id']}）.md")) if business_root.exists() else []
        if len(raw_matches) != 1 or len(business_matches) != 1:
            invalid_raw_navigation.append(row["source_id"])
            continue
        raw_text_page = raw_matches[0].read_text(encoding="utf-8", errors="replace")
        business_text_page = business_matches[0].read_text(encoding="utf-8", errors="replace")
        if (
            "打开业务分类目录中的对应知识页" not in raw_text_page
            or "打开完整原文镜像（按原目录）" not in business_text_page
            or "## AI检索元数据（17字段与蒸馏画像）" not in raw_text_page
            or "## 相关知识" not in raw_text_page
            or "## Original Content" not in raw_text_page
        ):
            invalid_raw_navigation.append(row["source_id"])
        extracted_path = paths["extracted"] / f"{row['source_id']}.txt"
        expected_original = common.sanitize_transient_urls(
            extracted_path.read_text(encoding="utf-8", errors="replace") if extracted_path.exists() else "未生成文本抽取结果。"
        ).rstrip()
        marker = "## Original Content\n\n"

        def contains_complete_original(page_text: str) -> bool:
            if marker not in page_text:
                return False
            remainder = page_text.split(marker, 1)[1]
            if not remainder.startswith(expected_original):
                return False
            tail = remainder[len(expected_original):]
            return not tail.strip() or tail.startswith("\n\n## Original Attachment\n\n")

        if not contains_complete_original(raw_text_page) or not contains_complete_original(business_text_page):
            invalid_original_content.append(row["source_id"])
    checks.append(("原目录与业务目录均为完整蒸馏知识页", not invalid_raw_navigation, f"paired_pages={len(successes)} invalid={len(invalid_raw_navigation)}"))
    checks.append(("两套目录均保留完整原文", not invalid_original_content, f"paired_pages={len(successes)} invalid={len(invalid_original_content)}"))
    passed = all(result for _, result, _ in checks)
    lines = ["# 知识库蒸馏本地验收", "", f"- 盘点总数：{len(manifest) + len(excluded)}", f"- Raw准入：{len(manifest)}", f"- 经Raw标准排除：{len(excluded)}", f"- 结论：{'通过' if passed else '未通过'}", f"- 成功：{len(successes)}", f"- 失败：{len(failures)}", f"- 待处理：{len(pending)}", f"- 关系候选：{len(candidates)}", f"- L2 抽样：{len(sampling)}", f"- L3 待业务确认：{len(queue)}", "", "## 检查项", ""]
    for name, result, detail in checks:
        lines.append(f"- [{'x' if result else ' '}] {name}：{detail}")
    lines.append("")
    common.atomic_write(paths["reports"] / "local-acceptance.md", "\n".join(lines))
    common.write_json(paths["reports"] / "local-acceptance.json", {"passed": passed, "checks": [{"name": name, "passed": result, "detail": detail} for name, result, detail in checks]})
    print(f"LOCAL_ACCEPTANCE_{'OK' if passed else 'FAILED'} checks={len(checks)} failed={sum(not result for _, result, _ in checks)}")
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
