#!/usr/bin/env python3
from __future__ import annotations

from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlsplit, urlunsplit


STATE_VERSION = 1
SENSITIVE_URL_KEYS = {
    "accesskeyid", "ossaccesskeyid", "signature", "expires", "security-token",
    "x-oss-security-token", "x-oss-signature", "x-oss-credential", "token", "access_token",
}
SOURCE_FIELDS = (
    "source_id", "department", "source_path", "file_name", "node_id", "source_url",
    "node_type", "content_type", "extension", "create_time", "update_time",
    "creator_uid", "creator_name", "owner", "permission_snapshot", "permission_hash",
    "virtual_kind", "parent_source_id", "parent_node_id", "resource_id",
    "path_hash", "metadata_hash", "observed_at", "snapshot_status",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def run_id() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S%z")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fingerprint(*values: object) -> str:
    return sha256_text(json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def canonical_hash(value: Any) -> str:
    """Return a deterministic SHA-256 for a JSON-compatible value."""
    return sha256_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def sanitize_transient_url(value: object) -> str:
    """Strip temporary credential parameters before writing audit artifacts."""
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
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def is_parent_managed_embedded_resource(source_id: object, state: dict | None = None) -> bool:
    """Embedded attachments are discovered by reading their parent, not by tree scans."""
    value = str(source_id or "")
    kind = str((state or {}).get("virtual_kind") or "")
    return kind == "embedded_attachment" or "-ATT-" in value


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
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_csv(path: Path, rows: list[dict], fieldnames: Iterable[str] | None = None) -> None:
    fields = list(fieldnames or [])
    if not fields:
        for row in rows:
            for key in row:
                if key not in fields:
                    fields.append(key)
    if not fields:
        fields = ["status"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", delete=False, dir=path.parent) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        temporary = Path(handle.name)
    temporary.replace(path)


def append_jsonl(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def parse_scalar(value: str) -> Any:
    value = value.split("#", 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    lowered = value.casefold()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none", "~"}:
        return None
    if re.fullmatch(r"[-+]?\d+", value):
        return int(value)
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)", value):
        return float(value)
    return value


def load_flat_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    values: dict[str, Any] = {}
    stack: list[tuple[int, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or raw.strip().startswith("-"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        while stack and indent <= stack[-1][0]:
            stack.pop()
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s*(.*))?$", raw.strip())
        if not match:
            continue
        key, raw_value = match.group(1), match.group(2) or ""
        prefix = ".".join(item[1] for item in stack)
        full = f"{prefix}.{key}" if prefix else key
        if raw_value.strip():
            values[full] = parse_scalar(raw_value)
        else:
            stack.append((indent, key))
    return values


def config_value(job: Path, key: str, default: Any = None) -> Any:
    values = load_flat_yaml(job / "00-config" / "maintenance-config.yaml")
    return values.get(key, default)


def task_value(job: Path, key: str, default: Any = None) -> Any:
    values = load_flat_yaml(job / "00-config" / "task-config.yaml")
    return values.get(key, default)


def bool_value(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().casefold() in {"true", "yes", "on", "1", "enabled", "allowed"}


def state_paths(job: Path) -> dict[str, Path]:
    root = job.resolve() / "05-ledgers" / "incremental"
    return {
        "root": root,
        "snapshots": root / "snapshots",
        "latest": root / "latest-observed.json",
        "observation": root / "observation-state.json",
        "applied": root / "applied-state.json",
        "plan": root / "change-plan.json",
        "orphans": root / "source-orphans.json",
        "health_state": root / "health-observation-state.json",
        "health": root / "health-report.json",
        "lock": root / ".maintenance.lock",
        "reports": job.resolve() / "06-reports",
    }


def find_base_skill(explicit: str = "") -> Path:
    candidates = [
        explicit,
        os.environ.get("DEPARTMENT_KB_DISTILL_ROOT", ""),
        str(Path(__file__).resolve().parents[2] / "department-kb-distill"),
    ]
    for value in candidates:
        if not value:
            continue
        path = Path(value).expanduser().resolve()
        contract = path / "schemas" / "maintenance-contract-v1.json"
        payload = load_json(contract, {})
        if path.exists() and isinstance(payload, dict) and payload.get("version") == 1:
            return path
    raise RuntimeError("未找到兼容的department-kb-distill；请安装同级Skill或传入--base-skill-root")


def run_command(command: list[str], *, timeout: int = 3600, capture: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, capture_output=capture, timeout=timeout)
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()[-2000:]
        raise RuntimeError(f"命令失败 exit={result.returncode}: {' '.join(command[:5])}\n{message}")
    return result


def run_base(base: Path, script: str, args: list[str], *, timeout: int = 7200) -> None:
    run_command([sys.executable, str(base / "scripts" / script), *args], timeout=timeout)


def extract_json(stdout: str) -> dict:
    start, end = stdout.find("{"), stdout.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError("命令未返回JSON")
    value = json.loads(stdout[start:end + 1])
    if not isinstance(value, dict):
        raise RuntimeError("命令JSON不是对象")
    return value


def document_state(row: dict, *, observed_at: str = "") -> dict:
    permission = str(row.get("permission_snapshot") or "")
    path_hash = str(row.get("path_hash") or fingerprint(row.get("source_path", "")))
    metadata_hash = str(row.get("metadata_hash") or fingerprint(
        row.get("file_name", ""), row.get("source_path", ""), row.get("extension", ""),
        row.get("content_type", ""), row.get("creator_uid", ""), row.get("update_time", ""),
    ))
    return {
        **{field: row.get(field, "") for field in SOURCE_FIELDS},
        "path_hash": path_hash,
        "metadata_hash": metadata_hash,
        "permission_hash": str(row.get("permission_hash") or (sha256_text(permission) if permission else "")),
        "source_hash": row.get("source_hash", ""),
        "extracted_hash": row.get("extracted_hash", ""),
        "parse_status": row.get("parse_status", ""),
        "processing": row.get("processing", ""),
        "status": row.get("status", ""),
        "last_seen_at": observed_at or row.get("observed_at", "") or now_iso(),
        "missing_count": int(row.get("missing_count") or 0),
    }


def parse_datetime(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (ValueError, OSError):
            return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@contextmanager
def task_lock(job: Path):
    path = state_paths(job)["lock"]
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise RuntimeError(f"该任务已有增量维护进程：{path}") from error
    try:
        os.write(descriptor, f"pid={os.getpid()} started_at={now_iso()}\n".encode())
        os.close(descriptor)
        yield
    finally:
        path.unlink(missing_ok=True)
