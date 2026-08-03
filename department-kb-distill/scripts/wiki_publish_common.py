#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Iterable
import unicodedata
from urllib.parse import unquote, urlsplit

from config_utils import load_config, publication_target


PUBLISH_VIEW_NAMES = (
    "01-原文镜像（按原目录）",
    "02-蒸馏结果（按文档类型）",
)
ROOT_PUBLICATION_FILES = (
    "00-AI问答与检索入口.md",
    "00-AI知识库地图.md",
)
STATE_NAME = "钉钉发布状态.json"
PUBLISH_CACHE_NAME = "钉钉发布内容"
NODE_RE = re.compile(r"/i/nodes/([^/?#]+)")
STABLE_ID_RE = re.compile(r"[A-Z][A-Z0-9_-]*-[A-F0-9]{12}", re.I)
MARKDOWN_LINK_RE = re.compile(r"(?P<label>!?\[[^\]\n]*\])\((?P<target>[^)\n]+)\)")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def node_id_from_url(value: str) -> str:
    match = NODE_RE.search(value or "")
    return match.group(1) if match else value.strip()


def doc_url(node_id: str) -> str:
    return f"https://alidocs.dingtalk.com/i/nodes/{node_id}"


def parse_json_output(output: str) -> dict[str, Any]:
    """Parse the last top-level JSON object, tolerating DWS progress lines."""
    decoder = json.JSONDecoder()
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for index, character in enumerate(output):
        if character != "{":
            continue
        try:
            value, consumed = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append((index, index + consumed, value))
    if not candidates:
        raise RuntimeError(f"DWS未返回可解析JSON：{output[-500:]}")
    top_level = [
        candidate
        for candidate in candidates
        if not any(
            other_start < candidate[0] and candidate[1] <= other_end
            for other_start, other_end, _ in candidates
        )
    ]
    return max(top_level, key=lambda candidate: candidate[0])[2]


def safe_message(value: str) -> str:
    value = re.sub(r"https?://\S+", "[URL]", value)
    value = re.sub(
        r"(?i)(api[_-]?key|authorization|bearer|token|secret|client[_-]?secret)\s*[:=]?\s*\S+",
        r"\1=[REDACTED]",
        value,
    )
    return value[-1200:]


def run_dws(
    args: list[str],
    *,
    attempts: int = 3,
    timeout_seconds: int = 180,
) -> tuple[dict[str, Any], float, int]:
    last_error = "unknown"
    started = time.monotonic()
    for attempt in range(1, attempts + 1):
        command = ["dws", *args, "--format", "json", "--timeout", str(timeout_seconds)]
        if attempt > 1:
            command.append("--verbose")
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds + 30)
            if result.returncode != 0:
                raise RuntimeError(f"exit={result.returncode}; {result.stderr or result.stdout}")
            payload = parse_json_output(result.stdout)
            if payload.get("success") is False:
                raise RuntimeError(json.dumps(payload, ensure_ascii=False))
            return payload, round(time.monotonic() - started, 3), attempt
        except Exception as error:  # noqa: BLE001 - retry boundary
            last_error = safe_message(str(error))
            if attempt < attempts:
                time.sleep((1, 3)[min(attempt - 1, 1)])
    raise RuntimeError(f"DWS连续{attempts}次失败：{last_error}")


def verify_publication_config(job: Path, target_url: str) -> dict[str, Any]:
    """Use the final folder URL in the initial task config as publication authority."""
    config = load_config(job)
    expected_target = publication_target(config)
    if not expected_target:
        raise RuntimeError("任务启动配置未启用 publishing 或未提供 target_folder_url")
    if node_id_from_url(expected_target) != node_id_from_url(target_url):
        raise RuntimeError("发布目标与任务启动时配置的 target_folder_url 不一致")
    allowed_views = config.get_list("publishing.allowed_source_views")
    if allowed_views and allowed_views != list(PUBLISH_VIEW_NAMES):
        raise RuntimeError("来源发布范围必须严格限定为两套知识视图；根目录AI入口由Skill固定生成")
    dangerous = {
        "allow_delete": config.bool("publishing.allow_delete", default=False),
        "allow_move": config.bool("publishing.allow_move", default=False),
        "allow_permission_change": config.bool("publishing.allow_permission_change", default=False),
        "allow_overwrite_existing": config.bool("publishing.allow_overwrite_existing", default=False),
    }
    excessive = [key for key, enabled in dangerous.items() if enabled]
    if excessive:
        raise RuntimeError(f"本程序不接受扩大危险权限：{', '.join(excessive)}")
    if not (job / "06-reports" / "local-acceptance.json").exists():
        raise RuntimeError("缺少本地验收结果，禁止发布")
    acceptance = load_json(job / "06-reports" / "local-acceptance.json", {})
    if acceptance.get("passed") is not True:
        raise RuntimeError("本地验收未通过，禁止发布")
    return {"target_url": expected_target, "allowed_source_views": list(PUBLISH_VIEW_NAMES), **{key: False for key in dangerous}}


@dataclass(frozen=True)
class PublicationPage:
    relative_path: str
    source_path: Path
    relative_directory: str
    title: str
    stable_id: str
    size_bytes: int
    source_sha256: str
    is_index: bool


def title_from_markdown(path: Path, text: str) -> str:
    for line in text.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return path.stem


def discover_pages(preview_root: Path) -> list[PublicationPage]:
    pages: list[PublicationPage] = []
    publication_paths: list[Path] = []
    for root_name in ROOT_PUBLICATION_FILES:
        root_page = preview_root / root_name
        if not root_page.is_file():
            raise RuntimeError(f"缺少根目录AI发布页：{root_page}")
        publication_paths.append(root_page)
    for view_name in PUBLISH_VIEW_NAMES:
        view = preview_root / view_name
        if not view.is_dir():
            raise RuntimeError(f"缺少发布视图：{view}")
        publication_paths.extend(sorted(view.rglob("*.md")))
    for path in publication_paths:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        relative = path.relative_to(preview_root).as_posix()
        is_index = path.name == "目录索引.md" or path.name in ROOT_PUBLICATION_FILES
        stable = None if is_index else (STABLE_ID_RE.search(relative) or STABLE_ID_RE.search(text))
        pages.append(
            PublicationPage(
                relative_path=relative,
                source_path=path,
                relative_directory=path.parent.relative_to(preview_root).as_posix(),
                title=title_from_markdown(path, text),
                stable_id=stable.group(0).upper() if stable else "",
                size_bytes=len(raw),
                source_sha256=sha256_bytes(raw),
                is_index=is_index,
            )
        )
    return pages


def choose_smoke_pages(pages: list[PublicationPage], group_count: int) -> list[PublicationPage]:
    if group_count <= 0:
        return pages
    groups: dict[str, list[PublicationPage]] = {}
    for page in pages:
        if page.stable_id and not page.is_index:
            groups.setdefault(page.stable_id, []).append(page)
    ranked = sorted(groups.values(), key=lambda values: max(page.size_bytes for page in values))
    if not ranked:
        raise RuntimeError("无可用于烟雾测试的成功文档对")
    positions: list[int]
    if group_count == 1:
        positions = [len(ranked) // 2]
    else:
        positions = [round(index * (len(ranked) - 1) / (group_count - 1)) for index in range(group_count)]
    selected_ids = {ranked[position][0].stable_id for position in positions}
    return [page for page in pages if page.stable_id in selected_ids]


def remove_first_h1(text: str, title: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if re.match(r"^#\s+", line):
            del lines[index]
            if index < len(lines) and not lines[index].strip():
                del lines[index]
        break
    return "\n".join(lines).rstrip() + "\n"


def publication_banner(text: str) -> str:
    replacements = {
        "> 本页为本地实验预览（业务分类视图），不会发布到钉钉知识库。": "> 本页为知识库蒸馏结果（业务分类视图）；完整原文、来源与关联关系均可追溯。",
        "> 本页为本地实验预览（原目录视图），不会发布到钉钉知识库。": "> 本页为知识库蒸馏结果（原目录视图）；完整原文、来源与关联关系均可追溯。",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = re.sub(
        r"> 本页为知识库蒸馏审核预览（业务分类视图）；[^\n]*",
        "> 本页为知识库蒸馏结果（业务分类视图）；完整原文、来源与关联关系均可追溯。",
        text,
    )
    text = re.sub(
        r"> 本页为知识库蒸馏审核预览（原目录视图）；[^\n]*",
        "> 本页为知识库蒸馏结果（原目录视图）；完整原文、来源与关联关系均可追溯。",
        text,
    )
    return text


def page_url_map(preview_root: Path, state: dict[str, Any]) -> dict[Path, str]:
    result: dict[Path, str] = {}
    for relative, record in (state.get("documents") or {}).items():
        if record.get("node_id"):
            result[(preview_root / relative).resolve()] = record.get("doc_url") or doc_url(record["node_id"])
    return result


def rewrite_local_links(
    text: str,
    *,
    page: PublicationPage,
    preview_root: Path,
    urls: dict[Path, str],
) -> tuple[str, list[str]]:
    unresolved: list[str] = []
    original_content_at = len(text)
    for marker in ("\n## Original Content", "\n## Original Attachment"):
        position = text.find(marker)
        if position >= 0:
            original_content_at = min(original_content_at, position)

    def replace(match: re.Match[str]) -> str:
        label = match.group("label")
        target = match.group("target").strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            return match.group(0)
        parsed = urlsplit(target)
        if parsed.scheme or not parsed.path:
            return match.group(0)
        resolved = (page.source_path.parent / unquote(parsed.path)).resolve()
        replacement = urls.get(resolved)
        if not replacement:
            # Original Content must stay faithful to the source. Relative links
            # copied from the source are preserved there, but only generated
            # navigation/relationship links before Original Content are gates.
            if parsed.path.lower().endswith(".md") and match.start() < original_content_at:
                unresolved.append(target)
            return match.group(0)
        suffix = f"#{parsed.fragment}" if parsed.fragment else ""
        return f"{label}({replacement}{suffix})"

    return MARKDOWN_LINK_RE.sub(replace, text), sorted(set(unresolved))


def render_page(
    page: PublicationPage,
    *,
    preview_root: Path,
    state: dict[str, Any],
) -> tuple[str, list[str]]:
    text = page.source_path.read_text(encoding="utf-8")
    text = remove_first_h1(text, page.title)
    text = publication_banner(text)
    text = re.sub(
        r"\n## 待业务确认的相关知识（确认前不会发布）\n.*?(?=\n## Original Content\n|\n## Original Attachment\n)",
        "\n",
        text,
        flags=re.S,
    )
    text, unresolved = rewrite_local_links(
        text,
        page=page,
        preview_root=preview_root,
        urls=page_url_map(preview_root, state),
    )
    return text, unresolved


def sanitize_dangerous_unicode(text: str) -> tuple[str, dict[str, int]]:
    """Remove invisible/control code points rejected by DingTalk's validator."""
    removed: dict[str, int] = {}
    result: list[str] = []
    for character in text:
        category = unicodedata.category(character)
        if category.startswith("C") and character not in {"\n", "\t"}:
            label = f"U+{ord(character):04X} {unicodedata.name(character, 'UNKNOWN')}"
            removed[label] = removed.get(label, 0) + 1
            continue
        result.append(character)
    return "".join(result), removed


def extract_markdown(payload: dict[str, Any]) -> str:
    preferred: list[str] = []
    fallback: list[str] = []

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, child_key.casefold())
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif isinstance(value, str):
            if key in {"content", "markdown", "body", "text"}:
                preferred.append(value)
            elif len(value) > 100:
                fallback.append(value)

    walk(payload)
    values = preferred or fallback
    if not values:
        raise RuntimeError("回读结果中未找到Markdown正文")
    return max(values, key=len)


def split_markdown_at_block_boundaries(text: str, max_characters: int = 9000) -> list[str]:
    """Split below DingTalk's 10k request cap without cutting a paragraph/block."""
    lines = text.splitlines(keepends=True)
    blocks: list[str] = []
    current_lines: list[str] = []
    fence: str | None = None
    for line in lines:
        stripped = line.lstrip()
        fence_match = re.match(r"(`{3,}|~{3,})", stripped)
        if fence is not None:
            current_lines.append(line)
            if fence_match and fence_match.group(1).startswith(fence[0]) and len(fence_match.group(1)) >= len(fence):
                blocks.append("".join(current_lines))
                current_lines = []
                fence = None
            continue
        if fence_match:
            if current_lines:
                blocks.append("".join(current_lines))
            current_lines = [line]
            fence = fence_match.group(1)
            continue
        current_lines.append(line)
        if not line.strip():
            blocks.append("".join(current_lines))
            current_lines = []
    if current_lines:
        blocks.append("".join(current_lines))

    safe_blocks: list[str] = []
    for block in blocks:
        if len(block) <= max_characters:
            safe_blocks.append(block)
            continue
        if re.search(r"(?m)^\s*(`{3,}|~{3,})", block):
            raise RuntimeError(
                f"Markdown代码块{len(block)}字符，超过{max_characters}字符安全分片上限"
            )
        table_lines = [line for line in block.splitlines() if line.count("|") >= 2]
        if len(table_lines) >= 2:
            raise RuntimeError(
                f"Markdown表格块{len(block)}字符，超过{max_characters}字符安全分片上限"
            )
        # A few imported Office/HTML sources contain tens of thousands of
        # characters without blank lines. They are plain extracted lines, so
        # use newline boundaries while still refusing to cut a single line.
        line_chunk = ""
        for line in block.splitlines(keepends=True):
            if len(line) > max_characters:
                raise RuntimeError(
                    f"Markdown单行{len(line)}字符，超过{max_characters}字符安全分片上限"
                )
            if line_chunk and len(line_chunk) + len(line) > max_characters:
                safe_blocks.append(line_chunk)
                line_chunk = line
            else:
                line_chunk += line
        if line_chunk:
            safe_blocks.append(line_chunk)

    chunks: list[str] = []
    current = ""
    for block in safe_blocks:
        if current and len(current) + len(block) > max_characters:
            chunks.append(current)
            current = block
        else:
            current += block
    if current:
        chunks.append(current)
    if not chunks:
        chunks = [""]
    if any(len(chunk) > max_characters for chunk in chunks):
        raise RuntimeError("Markdown分片超过安全上限")
    return chunks


def chunked(values: list[Any], size: int) -> Iterable[list[Any]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]
