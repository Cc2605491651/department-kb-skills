#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import re
import time
from typing import Any
import unicodedata

from wiki_publish_common import (
    STATE_NAME,
    atomic_write,
    extract_markdown,
    load_json,
    node_id_from_url,
    now_iso,
    run_dws,
    sha256_text,
    verify_publication_config,
    write_json,
)


REQUIRED_FIELDS = (
    "title", "page_type", "scenario", "keywords", "summary", "owner", "status", "processing",
    "version", "source", "source_updated_at", "content_hash", "sources", "blindspots", "relations",
    "property_generated_at", "property_updated_at",
)
AI_HEADER_FIELDS = ("schema_version", "stable_id", "view", "metadata", "distillation_profile")
DISTILLATION_PROFILE_FIELDS = (
    "core_theme", "business_objects", "document_role", "inputs", "actions", "outputs", "constraints",
)


def canonical(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).replace("\\", "")
    return "".join(character.casefold() for character in value if character.isalnum())


def final_visible_marker(sent: str) -> str:
    for line in reversed(sent.splitlines()):
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"^[#>*+\-\d.\s]+", "", line)
        marker = canonical(line)
        if len(marker) >= 4:
            return marker[-180:]
    return ""


def visible_canonical(value: str) -> str:
    value = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", value)
    return canonical(value)


def verify_content(
    record: dict[str, Any],
    sent: str,
    received: str,
    strict_links: bool,
    remote_title: str,
) -> dict[str, Any]:
    missing: list[str] = []
    if canonical(record.get("title", "")) != canonical(remote_title):
        missing.append("文档标题")
    is_index = bool(record.get("is_index"))
    if not is_index:
        first_visible = next((line.strip() for line in received.splitlines() if line.strip()), "")
        if "AI检索元数据（17字段与蒸馏画像）" not in first_visible:
            missing.append("统一AI检索元数据未位于页首")
        if not re.search(r"(?m)^\s*schema_version\s*:\s*[\"']?kb-ai-document-v2", received):
            missing.append("统一AI检索元数据版本")
        for field in AI_HEADER_FIELDS:
            if not re.search(rf"(?m)^\s*{re.escape(field)}\s*:", received):
                missing.append(f"统一AI检索元数据:{field}")
        for heading in ("Raw Mirror", "相关知识"):
            if canonical(heading) not in canonical(received):
                missing.append(heading)
        if canonical("Original Content") not in canonical(received) and canonical("Original Attachment") not in canonical(received):
            missing.append("Original Content/Attachment")
        for field in REQUIRED_FIELDS:
            if not re.search(rf"(?m)^\s*{re.escape(field)}\s*:", received):
                missing.append(f"17字段:{field}")
        for field in DISTILLATION_PROFILE_FIELDS:
            if not re.search(rf"(?m)^\s*{re.escape(field)}\s*:", received):
                missing.append(f"蒸馏画像:{field}")
        old_heading_patterns = {
            "AI检索卡（Agent优先读取）": r"(?m)^#{1,6}\s+AI检索卡（Agent优先读取）\s*$",
            "标准元数据（17字段）": r"(?m)^#{1,6}\s+标准元数据（17字段）\s*$",
            "蒸馏画像": r"(?m)^#{1,6}\s+蒸馏画像\s*$",
        }
        for old_heading, pattern in old_heading_patterns.items():
            if re.search(pattern, received):
                missing.append(f"重复旧区块:{old_heading}")
        content_hash = re.search(r"sha256:[a-fA-F0-9]{64}", sent)
        if content_hash and content_hash.group(0) not in received:
            missing.append("内容哈希")
    elif record.get("relative_path") == "00-AI问答与检索入口.md":
        if canonical("Agent固定路径") not in canonical(received):
            missing.append("Agent固定路径")
    elif record.get("relative_path") == "00-AI知识库地图.md":
        if canonical("按业务类型查找") not in canonical(received):
            missing.append("按业务类型查找")
    elif Path(str(record.get("relative_path") or "")).name == "目录索引.md":
        # DWS 把 Markdown 的首个 H1 作为文档标题保存，doc read 只返回正文，
        # 因此不能再要求正文重复出现“目录索引”。标题已在上方通过
        # remote_title 校验；正文只需保留每份目录页共有的结构锚点。
        if canonical("当前文件夹文档") not in canonical(received):
            missing.append("目录索引正文")
    sent_canonical = canonical(sent)
    received_canonical = canonical(received)
    tail = final_visible_marker(sent)
    if tail and tail not in visible_canonical(received):
        missing.append("文档末尾")
    length_ratio = len(received_canonical) / max(1, len(sent_canonical))
    if length_ratio < 0.85:
        missing.append("正文长度明显不足")
    if "不会发布到钉钉知识库" in received:
        missing.append("本地实验提示未替换")
    if strict_links and record.get("unresolved_local_links"):
        missing.append("未改写的本地相对链接")
    expected_node_ids = sorted(set(re.findall(r"alidocs\.dingtalk\.com/i/nodes/([^/?#)]+)", sent)))
    missing_links = [node_id for node_id in expected_node_ids if node_id not in received]
    if missing_links:
        missing.append(f"链接节点缺失:{len(missing_links)}")
    return {
        "passed": not missing,
        "missing": missing,
        "expected_link_nodes": len(expected_node_ids),
        "missing_link_nodes": missing_links,
        "sent_canonical_chars": len(sent_canonical),
        "received_canonical_chars": len(received_canonical),
        "received_to_sent_length_ratio": round(length_ratio, 4),
        "final_visible_marker": tail,
    }


def read_one(item: tuple[str, dict[str, Any]], strict_links: bool) -> tuple[str, dict[str, Any]]:
    key, record = item
    rendered_path = Path(record["rendered_path"])
    sent = rendered_path.read_text(encoding="utf-8")
    payload, elapsed, attempts = run_dws(["doc", "read", "--node", record["node_id"]])
    received = extract_markdown(payload)
    info_payload, info_elapsed, info_attempts = run_dws(["doc", "info", "--node", record["node_id"]])
    verification = verify_content(
        record,
        sent,
        received,
        strict_links,
        str(info_payload.get("name") or info_payload.get("title") or ""),
    )
    return key, {
        "readback_status": "success" if verification["passed"] else "failed",
        "readback_at": now_iso(),
        "readback_attempts": attempts + info_attempts,
        "readback_elapsed_seconds": round(elapsed + info_elapsed, 3),
        "readback_sha256": sha256_text(received),
        "readback_verification": verification,
    }


def list_folder_children(folder_node_id: str) -> set[str]:
    children: set[str] = set()
    page_token = ""
    while True:
        args = ["doc", "list", "--folder", folder_node_id, "--page-size", "50"]
        if page_token:
            args.extend(["--page-token", page_token])
        payload, _, _ = run_dws(args)
        for node in payload.get("nodes") or []:
            node_id = str(node.get("nodeId") or "")
            if node_id:
                children.add(node_id)
        if not payload.get("hasMore"):
            break
        page_token = str(payload.get("nextPageToken") or payload.get("pageToken") or "")
        if not page_token:
            raise RuntimeError("目录列表显示还有下一页，但未返回page token")
    return children


def audit_remote_tree(state: dict[str, Any], workers: int) -> dict[str, Any]:
    expected_by_parent: dict[str, set[str]] = {"": set()}
    folders = state.get("folders") or {}
    documents = state.get("documents") or {}
    for relative, record in folders.items():
        parent = Path(relative).parent.as_posix()
        if parent == ".":
            parent = ""
        expected_by_parent.setdefault(parent, set()).add(str(record["node_id"]))
        expected_by_parent.setdefault(relative, set())
    for record in documents.values():
        parent = str(record["folder"])
        if parent == ".":
            parent = ""
        expected_by_parent.setdefault(parent, set()).add(str(record["node_id"]))

    folder_nodes = {"": str(state["root"]["node_id"])}
    folder_nodes.update({relative: str(record["node_id"]) for relative, record in folders.items()})
    actual_by_parent: dict[str, set[str]] = {}
    errors: list[dict[str, str]] = []
    items = sorted(folder_nodes.items())
    for offset in range(0, len(items), 30):
        batch = items[offset:offset + 30]
        with ThreadPoolExecutor(max_workers=min(workers, len(batch))) as executor:
            futures = {executor.submit(list_folder_children, node_id): relative for relative, node_id in batch}
            for future in as_completed(futures):
                relative = futures[future]
                try:
                    actual_by_parent[relative] = future.result()
                except Exception as error:  # noqa: BLE001 - per-folder audit
                    errors.append({"folder": relative or "<root>", "error": str(error)})
    missing: list[dict[str, Any]] = []
    extra: list[dict[str, Any]] = []
    for relative, expected in expected_by_parent.items():
        actual = actual_by_parent.get(relative, set())
        if expected - actual:
            missing.append({"folder": relative or "<root>", "node_ids": sorted(expected - actual)})
        if actual - expected:
            extra.append({"folder": relative or "<root>", "node_ids": sorted(actual - expected)})
    return {
        "passed": not errors and not missing and not extra,
        "expected_folders": len(folders),
        "expected_documents": len(documents),
        "audited_parent_folders": len(folder_nodes),
        "missing": missing,
        "extra": extra,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read back and verify published DingTalk knowledge pages.")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--workers", type=int, default=30)
    parser.add_argument("--last-scope", action="store_true")
    parser.add_argument("--retry-failed-only", action="store_true")
    args = parser.parse_args()

    started = time.monotonic()
    job = args.job.resolve()
    workers = max(1, min(args.workers, 30))
    verify_publication_config(job, args.target)
    state_file = job / "05-ledgers" / STATE_NAME
    state = load_json(state_file, {}) or {}
    if node_id_from_url(str(state.get("target_url") or "")) != node_id_from_url(args.target):
        raise SystemExit("发布状态与回读目标不一致")
    scope = state.get("last_publish_scope") or {}
    strict_links = scope.get("mode") == "full"
    if args.last_scope:
        keys = scope.get("document_keys") or []
    else:
        keys = sorted(state.get("documents") or {})
    all_items = [
        (key, state["documents"][key])
        for key in keys
        if state["documents"][key].get("update_status") == "success"
    ]
    items = [item for item in all_items if not args.retry_failed_only or item[1].get("readback_status") != "success"]
    failures: list[dict[str, str]] = []
    for offset in range(0, len(items), 30):
        batch = items[offset:offset + 30]
        with ThreadPoolExecutor(max_workers=min(workers, len(batch))) as executor:
            futures = {executor.submit(read_one, item, strict_links): item for item in batch}
            for future in as_completed(futures):
                key, _ = futures[future]
                try:
                    _, result = future.result()
                    state["documents"][key].update(result)
                    if result["readback_status"] != "success":
                        failures.append({"relative_path": key, "error": str(result["readback_verification"]["missing"])})
                except Exception as error:  # noqa: BLE001 - per-page ledger
                    state["documents"][key].update(
                        {"readback_status": "failed", "readback_at": now_iso(), "readback_error": str(error)}
                    )
                    failures.append({"relative_path": key, "error": str(error)})
                state["updated_at"] = now_iso()
                write_json(state_file, state)

    tree_audit = audit_remote_tree(state, workers)
    remaining_failures: list[dict[str, str]] = []
    for key, record in all_items:
        if record.get("readback_status") != "success":
            verification = record.get("readback_verification") or {}
            remaining_failures.append(
                {
                    "relative_path": key,
                    "error": str(verification.get("missing") or record.get("readback_error") or "unknown"),
                }
            )
    report = {
        "generated_at": now_iso(),
        "target_url": state.get("target_url"),
        "root": state.get("root"),
        "checked_documents": len(all_items),
        "checked_this_run": len(items),
        "passed_documents": len(all_items) - len(remaining_failures),
        "failed_documents": len(remaining_failures),
        "strict_link_validation": strict_links,
        "tree_audit": tree_audit,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "failures": remaining_failures,
    }
    write_json(job / "06-reports" / "钉钉回读校验结果.json", report)
    root = report.get("root") or {}
    lines = [
        "# 钉钉回读校验结果",
        "",
        f"- 验收时间：{report['generated_at']}",
        f"- 发布目录：[{root.get('name', '')}]({root.get('doc_url', '')})",
        f"- 回读总数：{report['checked_documents']} 篇",
        f"- 本次实际回读：{report['checked_this_run']} 篇",
        f"- 通过：{report['passed_documents']} 篇",
        f"- 失败：{report['failed_documents']} 篇",
        f"- 链接严格校验：{'是' if strict_links else '否（烟雾测试）'}",
        f"- 远程目录树：{'通过' if tree_audit['passed'] else '失败'}"
        f"（{tree_audit['expected_folders']} 个子文件夹，{tree_audit['expected_documents']} 篇文档）",
        f"- 总耗时：{report['elapsed_seconds']} 秒",
    ]
    if remaining_failures:
        lines.extend(["", "## 回读失败", ""])
        lines.extend(f"- `{row['relative_path']}`：{row['error']}" for row in remaining_failures)
    if not tree_audit["passed"]:
        lines.extend(["", "## 目录树异常", "", f"```json\n{tree_audit}\n```"])
    atomic_write(job / "06-reports" / "钉钉回读校验结果.md", "\n".join(lines) + "\n")
    try:
        from build_delivery_summary import build as build_delivery_summary
        build_delivery_summary(job)
    except Exception as error:  # noqa: BLE001 - report refresh must be visible
        raise RuntimeError(f"回读已完成，但交付清单刷新失败：{error}") from error
    print(
        f"READBACK_ROOT={root.get('doc_url', '')} checked={len(all_items)} "
        f"success={len(all_items) - len(remaining_failures)} failed={len(remaining_failures)} "
        f"tree={'pass' if tree_audit['passed'] else 'fail'}"
    )
    if remaining_failures or not tree_audit["passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
