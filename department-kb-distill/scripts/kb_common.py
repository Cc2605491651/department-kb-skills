#!/usr/bin/env python3
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from config_utils import load_config


PROMPT_VERSION = "department-kb-semantic-v2"
RELATION_PROMPT_VERSION = "department-kb-relation-v4"
SUCCESS_STATUSES = {"全文已解析"}
LOG_LOCK = threading.Lock()
SENSITIVE_URL_KEYS = {
    "accesskeyid", "ossaccesskeyid", "signature", "expires", "security-token",
    "x-oss-security-token", "x-oss-signature", "x-oss-credential", "token", "access_token",
}


def load_task_config(job: Path):
    return load_config(job)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sanitize_transient_url(value: object, parent_node_id: object = "") -> str:
    """Remove temporary signed query parameters; embedded files point back to their parent doc."""
    url = str(value or "")
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        keys = {key.casefold() for key, _ in parse_qsl(parts.query, keep_blank_values=True)}
    except ValueError:
        return url
    if not keys.intersection(SENSITIVE_URL_KEYS):
        return url
    parent = str(parent_node_id or "")
    if parent:
        return f"https://alidocs.dingtalk.com/i/nodes/{parent}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def sanitize_transient_urls(value: object) -> str:
    """Redact signed URL query strings embedded in extracted plain text or Markdown."""
    text = str(value or "")
    pattern = re.compile(r"https?://[^\s<>\"']+")

    def replace(match: re.Match[str]) -> str:
        candidate = match.group(0)
        trailing = ""
        while candidate and candidate[-1] in ").,;:!?，。；：！？]}":
            trailing = candidate[-1] + trailing
            candidate = candidate[:-1]
        return sanitize_transient_url(candidate) + trailing

    return pattern.sub(replace, text)


def relation_type_explanation(value: str) -> str:
    meanings = {
        "明确引用": "来源文档正文中直接出现目标文档的链接或明确名称；这里只证明“确实引用”，不自动证明目标是当前有效或强制依据",
        "执行依据": "来源文档要求执行任务时参考目标文档中的规则、步骤或边界",
        "实施方案": "目标文档提供把来源文档中的目标落地为具体行动的方法或方案",
        "工作区导航": "来源文档把目标文档作为进入相关工作区或专题内容的入口",
        "契约依据": "目标文档被当作约束合作边界、职责、数据写入或验收方式的契约，而不只是普通参考材料",
        "数据结构依据": "目标文档定义实施时应采用的表、字段、数据关系或存储边界",
        "正式口径索引": "来源文档把目标文档列为当前正式规则或数据定义的入口，使用者会据此判断应执行哪套口径",
        "模板应用": "一份文档提供模板或固定方法，另一份文档实际使用该模板形成业务结果",
        "实施关系": "一份文档规定方法，另一份文档将该方法落实到具体业务中",
    }
    parts = [part.strip() for part in re.split(r"[/／]", value or "") if part.strip()]
    explanations = [f"“{part}”：{meanings[part]}" for part in parts if part in meanings]
    return "；".join(explanations) or f"系统将双方关系归类为“{value or '未分类'}”；具体业务含义以本条关系解释和双方证据为准"


def business_relation_name(value: str) -> str:
    names = {
        "明确引用/执行依据": "引用并作为执行依据",
        "明确引用/实施方案": "引用并作为实施方案",
        "明确引用/工作区导航": "引用并作为资料入口",
        "明确引用/契约依据": "引用并作为执行契约",
        "明确引用/数据结构依据": "引用并作为数据结构依据",
        "明确引用/正式口径索引": "引用并作为当前正式口径",
        "模板应用/实施关系": "使用模板并落地实施",
        "明确引用": "直接引用",
        "完全重复": "内容完全相同",
    }
    return names.get(value, value or "存在业务关联")


def atomic_write(path: Path, value: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if isinstance(value, bytes):
        temporary.write_bytes(value)
    else:
        temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", delete=False, dir=path.parent) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        temporary = Path(handle.name)
    temporary.replace(path)


def append_jsonl(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with LOG_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def job_paths(job: Path) -> dict[str, Path]:
    job = job.resolve()
    semantic = job / "02-extraction-cache" / "semantic"
    return {
        "job": job,
        "manifest": job / "01-inventory" / "raw-manifest.json",
        "extracted": job / "02-extraction-cache" / "extracted",
        "semantic": semantic,
        "content_profiles": semantic / "content-profiles",
        "source_profiles": semantic / "source-profiles",
        "chunk_profiles": semantic / "chunk-profiles",
        "schemas": semantic / "schemas",
        "ledgers": job / "05-ledgers",
        "reports": job / "06-reports",
    }


def load_manifest(job: Path) -> list[dict]:
    paths = job_paths(job)
    rows = load_json(paths["manifest"], [])
    if not isinstance(rows, list):
        raise RuntimeError("raw-manifest.json 不是数组")
    return rows


def accepted_document_hashes(acceptance: Any) -> dict[str, str]:
    if not isinstance(acceptance, dict) or acceptance.get("decision") != "confirmed":
        return {}
    documents = acceptance.get("documents")
    if not isinstance(documents, list):
        return {}
    return {
        str(item.get("source_id") or ""): str(item.get("content_hash") or "")
        for item in documents
        if isinstance(item, dict) and item.get("source_id") and item.get("content_hash")
    }


def document_business_status(profile: dict, acceptance: Any) -> str:
    accepted = accepted_document_hashes(acceptance)
    source_id = str(profile.get("source_id") or "")
    source_hash = str(profile.get("source_hash") or "")
    return "正式" if source_id and source_hash and accepted.get(source_id) == source_hash else "候选"


def safe_error(error: BaseException) -> str:
    message = str(error)
    message = re.sub(r"https?://\S+", "[URL]", message)
    message = re.sub(r"(?i)(api[_-]?key|authorization|bearer|token|secret)\s*[:=]?\s*\S+", r"\1=[REDACTED]", message)
    return f"{type(error).__name__}: {message[:1000]}"


def redact_sensitive(value: str) -> tuple[str, list[str]]:
    patterns = [
        ("访问密码", re.compile(r"(?i)((?:访问)?密码|password|passcode)(\s*[:：=]\s*)([^\s,，;；。]{3,})")),
        ("令牌或密钥", re.compile(r"(?i)(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|secret|bearer)(\s*[:：=]?\s*)([A-Za-z0-9_./+\-=]{6,})")),
        ("私钥", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ]
    warnings: list[str] = []
    redacted = value
    for label, pattern in patterns:
        if pattern.search(redacted):
            warnings.append(label)
            if pattern.groups >= 3:
                redacted = pattern.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", redacted)
            else:
                redacted = pattern.sub("[REDACTED PRIVATE KEY]", redacted)
    return redacted, warnings


def codex_version() -> str:
    result = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError("codex CLI 不可用")
    return result.stdout.strip()


def extract_usage(events: str) -> dict:
    usage: dict[str, Any] = {}
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        candidate = event.get("usage") or (event.get("item") or {}).get("usage")
        if isinstance(candidate, dict):
            usage = candidate
        if event.get("type") in {"turn.completed", "response.completed"}:
            nested = event.get("usage") or (event.get("response") or {}).get("usage")
            if isinstance(nested, dict):
                usage = nested
    return usage


def run_codex_structured(
    *,
    prompt: str,
    schema_path: Path,
    cwd: Path,
    model: str = "",
    timeout: int = 1200,
    attempts: int = 2,
) -> tuple[dict, dict]:
    last_error: BaseException | None = None
    cli_version = codex_version()
    for attempt in range(1, attempts + 1):
        with tempfile.TemporaryDirectory(prefix="kb-codex-") as temp_dir:
            output_path = Path(temp_dir) / "last-message.json"
            command = [
                "codex", "exec", "--ephemeral", "--sandbox", "read-only",
                "--skip-git-repo-check", "--json", "--output-schema", str(schema_path),
                "--output-last-message", str(output_path), "-C", str(cwd),
            ]
            if model:
                command.extend(["--model", model])
            command.append("-")
            started = now_iso()
            try:
                result = subprocess.run(command, input=prompt, capture_output=True, text=True, timeout=timeout)
                if result.returncode != 0:
                    raise RuntimeError(f"codex exec exit={result.returncode}: {result.stderr[-1000:]}")
                if not output_path.exists():
                    raise RuntimeError("codex exec 未生成结构化输出文件")
                payload = json.loads(output_path.read_text(encoding="utf-8"))
                metadata = {
                    "attempts": attempt,
                    "started_at": started,
                    "finished_at": now_iso(),
                    "codex_cli_version": cli_version,
                    "model": model or "default",
                    "usage": extract_usage(result.stdout),
                    "event_chars": len(result.stdout),
                }
                return payload, metadata
            except Exception as error:
                last_error = error
    raise RuntimeError(f"Codex 结构化任务失败：{safe_error(last_error or RuntimeError('unknown'))}")


def normalized_text(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"[\s\u3000]+", "", value)
    value = re.sub(r"[\W_]+", "", value, flags=re.UNICODE)
    return value


def normalized_title(value: str) -> str:
    value = Path(value).stem
    value = re.sub(r"\[[A-Z][A-Z0-9_-]+-[A-F0-9]{12}\]", "", value, flags=re.I)
    value = re.sub(r"(?i)(?:[-_｜| ]?(?:v(?:er(?:sion)?)?\s*\d+(?:\.\d+)*|新版|旧版|最新版|修订版|最终版|草稿版|\d{4}[-_/]\d{1,2}[-_/]\d{1,2}))", "", value)
    return normalized_text(value)


def tokenise(value: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,}|\d{2,}|[\u4e00-\u9fff]{2,}", value.casefold())
    result: list[str] = []
    for word in words:
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", word):
            result.extend(word[index:index + 2] for index in range(len(word) - 1))
        else:
            result.append(word)
    return result


def batched(values: list[Any], size: int) -> Iterable[list[Any]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]
