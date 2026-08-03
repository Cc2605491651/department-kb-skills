#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import re
import time
from typing import Any, Callable

from wiki_publish_common import (
    PUBLISH_CACHE_NAME,
    STATE_NAME,
    PublicationPage,
    choose_smoke_pages,
    discover_pages,
    doc_url,
    node_id_from_url,
    now_iso,
    render_page,
    run_dws,
    sanitize_dangerous_unicode,
    sha256_text,
    split_markdown_at_block_boundaries,
    verify_publication_config,
    write_json,
    atomic_write,
    load_json,
)


def state_path(job: Path) -> Path:
    return job / "05-ledgers" / STATE_NAME


def save_state(job: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    write_json(state_path(job), state)


def create_folder(name: str, parent: str) -> dict[str, Any]:
    payload, elapsed, attempts = run_dws(
        ["doc", "folder", "create", "--name", name, "--folder", parent, "--yes"]
    )
    node_id = str(payload.get("nodeId") or payload.get("node_id") or "")
    if not node_id:
        raise RuntimeError(f"创建文件夹未返回nodeId：{name}")
    return {
        "name": name,
        "node_id": node_id,
        "doc_url": str(payload.get("docUrl") or doc_url(node_id)),
        "elapsed_seconds": elapsed,
        "attempts": attempts,
        "created_at": now_iso(),
    }


def create_empty_document(page: PublicationPage, folder_id: str) -> tuple[str, dict[str, Any]]:
    payload, elapsed, attempts = run_dws(
        ["doc", "create", "--name", page.title, "--folder", folder_id, "--content-format", "markdown", "--yes"]
    )
    node_id = str(payload.get("nodeId") or payload.get("node_id") or "")
    if not node_id:
        raise RuntimeError(f"创建文档未返回nodeId：{page.relative_path}")
    return page.relative_path, {
        "relative_path": page.relative_path,
        "title": page.title,
        "stable_id": page.stable_id,
        "is_index": page.is_index,
        "folder": page.relative_directory,
        "node_id": node_id,
        "doc_url": str(payload.get("docUrl") or doc_url(node_id)),
        "source_sha256": page.source_sha256,
        "source_size_bytes": page.size_bytes,
        "create_status": "success",
        "create_attempts": attempts,
        "create_elapsed_seconds": elapsed,
        "created_at": now_iso(),
    }


def publication_parent_id(page: PublicationPage, state: dict[str, Any]) -> str:
    if page.relative_directory in {"", "."}:
        return str(state["root"]["node_id"])
    return str(state["folders"][page.relative_directory]["node_id"])


def should_update_page(
    previous_status: str | None,
    previous_sha256: str | None,
    next_sha256: str,
    retry_failed_only: bool,
) -> bool:
    """Return whether an existing publication record requires a remote write."""
    if retry_failed_only and previous_status == "success":
        return False
    if previous_status == "success" and previous_sha256 == next_sha256:
        return False
    return True


def update_document(page: PublicationPage, record: dict[str, Any], rendered_path: Path) -> tuple[str, dict[str, Any]]:
    rendered = rendered_path.read_text(encoding="utf-8")
    chunks = split_markdown_at_block_boundaries(rendered, max_characters=9000)
    total_elapsed = 0.0
    total_attempts = 0
    dws_chunks_written = 0
    parts_directory = rendered_path.parent / f".{rendered_path.name}.parts"
    parts_directory.mkdir(parents=True, exist_ok=True)
    for index, chunk in enumerate(chunks):
        part_path = parts_directory / f"part-{index + 1:03d}-of-{len(chunks):03d}.md"
        atomic_write(part_path, chunk)
        payload, elapsed, attempts = run_dws(
            [
                "doc", "update", "--node", record["node_id"], "--content-file", str(part_path),
                "--mode", "overwrite" if index == 0 else "append", "--content-format", "markdown", "--yes",
            ]
        )
        total_elapsed += elapsed
        total_attempts += attempts
        dws_chunks_written += int(payload.get("chunksWritten", 1) or 1)
    return page.relative_path, {
        "update_status": "success",
        "update_attempts": total_attempts,
        "update_elapsed_seconds": round(total_elapsed, 3),
        "manual_chunks_written": len(chunks),
        "chunks_written": dws_chunks_written,
        "published_at": now_iso(),
    }


def run_parallel(
    items: list[Any],
    *,
    workers: int,
    operation: Callable[[Any], tuple[str, dict[str, Any]]],
    on_success: Callable[[str, dict[str, Any]], None],
    on_failure: Callable[[Any, Exception], None],
) -> None:
    for offset in range(0, len(items), 30):
        batch = items[offset:offset + 30]
        with ThreadPoolExecutor(max_workers=min(workers, len(batch))) as executor:
            futures = {executor.submit(operation, item): item for item in batch}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    key, result = future.result()
                    on_success(key, result)
                except Exception as error:  # noqa: BLE001 - per-page ledger
                    on_failure(item, error)


def write_report(job: Path, state: dict[str, Any], selected: list[PublicationPage], started: float) -> None:
    documents = state.get("documents") or {}
    selected_records = [documents.get(page.relative_path, {}) for page in selected]
    failed = [record for record in selected_records if record.get("update_status") != "success"]
    report = {
        "generated_at": now_iso(),
        "target_url": state.get("target_url"),
        "root": state.get("root"),
        "selected_documents": len(selected),
        "created_documents": sum(record.get("create_status") == "success" for record in selected_records),
        "published_documents": sum(record.get("update_status") == "success" for record in selected_records),
        "updated_this_run": len((state.get("last_publish_scope") or {}).get("document_keys") or []),
        "unchanged_this_run": len((state.get("last_publish_scope") or {}).get("unchanged_document_keys") or []),
        "failed_documents": len(failed),
        "unresolved_link_documents": sum(bool(record.get("unresolved_local_links")) for record in selected_records),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "failures": [
            {"relative_path": record.get("relative_path"), "error": record.get("last_error")}
            for record in failed
        ],
    }
    write_json(job / "06-reports" / "钉钉发布结果.json", report)
    root = report.get("root") or {}
    lines = [
        "# 钉钉发布结果",
        "",
        f"- 发布时间：{report['generated_at']}",
        f"- 目标目录：[{root.get('name', '未创建')}]({root.get('doc_url', state.get('target_url', ''))})",
        f"- 本次范围：{report['selected_documents']} 篇",
        f"- 已写入：{report['published_documents']} 篇",
        f"- 本次实际更新：{report['updated_this_run']} 篇",
        f"- 内容未变化、跳过写入：{report['unchanged_this_run']} 篇",
        f"- 失败：{report['failed_documents']} 篇",
        f"- 尚有本地相对链接的文档：{report['unresolved_link_documents']} 篇",
        f"- 总耗时：{report['elapsed_seconds']} 秒",
        "",
        "> 发布成功不等于验收成功；必须继续执行回读程序。",
    ]
    if failed:
        lines.extend(["", "## 失败文档", ""])
        lines.extend(f"- `{row['relative_path']}`：{row['error']}" for row in report["failures"])
    atomic_write(job / "06-reports" / "钉钉发布结果.md", "\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish distilled knowledge pages to a DingTalk folder.")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--root-name", required=True)
    parser.add_argument("--workers", type=int, default=30)
    parser.add_argument("--smoke-groups", type=int, default=0)
    parser.add_argument("--retry-failed-only", action="store_true")
    args = parser.parse_args()

    started = time.monotonic()
    job = args.job.resolve()
    workers = max(1, min(args.workers, 30))
    authorization = verify_publication_config(job, args.target)
    target_info, _, _ = run_dws(["doc", "info", "--node", args.target])
    if target_info.get("nodeType") != "folder":
        raise SystemExit("目标节点不是文档文件夹")
    if node_id_from_url(args.target) != str(target_info.get("nodeId")):
        raise SystemExit("目标节点探测结果与请求不一致")

    preview_root = job / "本地审核结果（仅供审核）"
    all_pages = discover_pages(preview_root)
    selected = choose_smoke_pages(all_pages, args.smoke_groups)
    selected_by_path = {page.relative_path: page for page in selected}

    state = load_json(state_path(job), {}) or {}
    if state and node_id_from_url(str(state.get("target_url") or "")) != node_id_from_url(args.target):
        raise SystemExit("已有发布状态属于其他目标，为防止误发已停止")
    if state.get("root") and state["root"].get("name") != args.root_name:
        old_root = state["root"]
        safe_old_name = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", str(old_root.get("name") or "old-root"))[:80]
        archive_name = f"{safe_old_name}-{str(old_root.get('node_id') or '')[:12]}.json"
        write_json(job / "05-ledgers" / "历史发布状态" / archive_name, state)
        state = {
            "previous_publication": {
                "name": old_root.get("name"),
                "node_id": old_root.get("node_id"),
                "doc_url": old_root.get("doc_url"),
                "archived_state": archive_name,
            }
        }
    state.setdefault("version", 1)
    state["target_url"] = args.target
    state["target_node_id"] = target_info.get("nodeId")
    state["target_workspace_id"] = target_info.get("workspaceId")
    state["publication_authority"] = "task-config.yaml:publishing.target_folder_url"
    state["publication_config_sha256"] = sha256_text(str(authorization))
    state.setdefault("folders", {})
    state.setdefault("documents", {})

    if not state.get("root"):
        state["root"] = create_folder(args.root_name, str(target_info["nodeId"]))
        save_state(job, state)

    required_directories: set[str] = set()
    for page in selected:
        directory = Path(page.relative_directory)
        current = Path()
        for part in directory.parts:
            if part in {"", "."}:
                continue
            current /= part
            required_directories.add(current.as_posix())
    for relative_directory in sorted(required_directories, key=lambda value: (len(Path(value).parts), value)):
        if relative_directory in state["folders"]:
            continue
        parent_relative = Path(relative_directory).parent.as_posix()
        parent_id = state["root"]["node_id"] if parent_relative == "." else state["folders"][parent_relative]["node_id"]
        state["folders"][relative_directory] = create_folder(Path(relative_directory).name, parent_id)
        save_state(job, state)

    to_create = [page for page in selected if not (state["documents"].get(page.relative_path) or {}).get("node_id")]

    def created(key: str, record: dict[str, Any]) -> None:
        state["documents"][key] = record
        save_state(job, state)

    def create_failed(item: PublicationPage, error: Exception) -> None:
        record = state["documents"].setdefault(item.relative_path, {"relative_path": item.relative_path})
        record.update({"create_status": "failed", "last_error": str(error), "failed_at": now_iso()})
        save_state(job, state)

    run_parallel(
        to_create,
        workers=workers,
        operation=lambda page: create_empty_document(page, publication_parent_id(page, state)),
        on_success=created,
        on_failure=create_failed,
    )

    cache_root = job / "02-extraction-cache" / PUBLISH_CACHE_NAME
    update_items: list[tuple[PublicationPage, dict[str, Any], Path]] = []
    unchanged_keys: list[str] = []
    strict_links = args.smoke_groups == 0
    for page in selected:
        record = state["documents"].get(page.relative_path) or {}
        if not record.get("node_id"):
            continue
        previous_update_status = record.get("update_status")
        previous_publication_sha256 = record.get("publication_sha256")
        rendered, unresolved = render_page(page, preview_root=preview_root, state=state)
        rendered, removed_unicode = sanitize_dangerous_unicode(rendered)
        publication_sha256 = sha256_text(rendered)
        rendered_path = cache_root / page.relative_path
        atomic_write(rendered_path, rendered)
        record.update(
            {
                "relative_path": page.relative_path,
                "title": page.title,
                "stable_id": page.stable_id,
                "source_sha256": page.source_sha256,
                "publication_sha256": publication_sha256,
                "rendered_path": str(rendered_path),
                "unresolved_local_links": unresolved,
                "removed_dangerous_unicode": removed_unicode,
            }
        )
        if strict_links and unresolved:
            record.update({"update_status": "blocked", "last_error": f"存在未解析本地链接：{unresolved[:5]}"})
        elif not should_update_page(
            str(previous_update_status) if previous_update_status else None,
            str(previous_publication_sha256) if previous_publication_sha256 else None,
            publication_sha256,
            args.retry_failed_only,
        ):
            record.update({"update_skipped": "unchanged_or_already_successful", "last_checked_at": now_iso()})
            unchanged_keys.append(page.relative_path)
            continue
        else:
            record.pop("update_skipped", None)
            update_items.append((page, record, rendered_path))
    save_state(job, state)

    def updated(key: str, result: dict[str, Any]) -> None:
        state["documents"][key].update(result)
        state["documents"][key].pop("last_error", None)
        state["documents"][key].pop("update_skipped", None)
        save_state(job, state)

    def update_failed(item: tuple[PublicationPage, dict[str, Any], Path], error: Exception) -> None:
        page = item[0]
        state["documents"][page.relative_path].update(
            {"update_status": "failed", "last_error": str(error), "failed_at": now_iso()}
        )
        save_state(job, state)

    run_parallel(
        update_items,
        workers=workers,
        operation=lambda item: update_document(*item),
        on_success=updated,
        on_failure=update_failed,
    )

    state["last_publish_scope"] = {
        "mode": "smoke" if args.smoke_groups else "full",
        "selected_document_keys": sorted(selected_by_path),
        "document_keys": sorted(item[0].relative_path for item in update_items),
        "unchanged_document_keys": sorted(unchanged_keys),
        "finished_at": now_iso(),
    }
    save_state(job, state)
    write_report(job, state, selected, started)
    from build_delivery_summary import build as build_delivery_summary
    build_delivery_summary(job)
    failures = [
        page.relative_path
        for page in selected
        if (state["documents"].get(page.relative_path) or {}).get("update_status") != "success"
    ]
    print(
        f"PUBLISH_ROOT={state['root']['doc_url']} selected={len(selected)} "
        f"updated={len(update_items)} unchanged={len(unchanged_keys)} "
        f"success={len(selected) - len(failures)} failed={len(failures)}"
    )
    if failures:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
