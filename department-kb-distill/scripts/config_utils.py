#!/usr/bin/env python3
"""Read the Skill's constrained YAML configuration without a hard PyYAML dependency."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any


def _strip_comment(value: str) -> str:
    quote = ""
    result: list[str] = []
    for character in value:
        if character in {"'", '"'}:
            if not quote:
                quote = character
            elif quote == character:
                quote = ""
        if character == "#" and not quote:
            break
        result.append(character)
    return "".join(result).strip()


def _scalar(value: str) -> Any:
    value = _strip_comment(value)
    if not value:
        return ""
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
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [] if not inner else [_scalar(part) for part in inner.split(",")]
    return value


@dataclass(frozen=True)
class Config:
    path: Path
    text: str
    values: dict[str, Any]
    lists: dict[str, list[Any]]

    def get(self, *paths: str, default: Any = "") -> Any:
        for path in paths:
            if path in self.values:
                return self.values[path]
        for path in paths:
            leaf = path.rsplit(".", 1)[-1]
            matches = [value for key, value in self.values.items() if key.rsplit(".", 1)[-1] == leaf]
            if len(matches) == 1:
                return matches[0]
        return default

    def get_list(self, *paths: str) -> list[Any]:
        for path in paths:
            if path in self.lists:
                return list(self.lists[path])
            value = self.values.get(path)
            if isinstance(value, list):
                return list(value)
        for path in paths:
            leaf = path.rsplit(".", 1)[-1]
            matches = [value for key, value in self.lists.items() if key.rsplit(".", 1)[-1] == leaf]
            if len(matches) == 1:
                return list(matches[0])
        return []

    def bool(self, *paths: str, default: bool = False) -> bool:
        value = self.get(*paths, default=default)
        if isinstance(value, bool):
            return value
        return str(value).strip().casefold() in {"true", "yes", "on", "1", "enabled", "allowed"}


def load_config(path_or_job: Path) -> Config:
    path = path_or_job
    if path.is_dir() or path.suffix.lower() not in {".yaml", ".yml"}:
        path = path / "00-config" / "task-config.yaml"
    if not path.exists():
        return Config(path=path, text="", values={}, lists={})
    text = path.read_text(encoding="utf-8")
    values: dict[str, Any] = {}
    lists: dict[str, list[Any]] = {}
    stack: list[tuple[int, str]] = []
    current_list_path = ""
    current_list_indent = -1
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if stripped.startswith("-"):
            if current_list_path and indent > current_list_indent:
                item = stripped[1:].strip()
                if item and ":" not in item:
                    lists.setdefault(current_list_path, []).append(_scalar(item))
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s*(.*))?$", stripped)
        if not match:
            continue
        key, raw_value = match.group(1), match.group(2) or ""
        prefix = ".".join(value for _, value in stack)
        full_path = f"{prefix}.{key}" if prefix else key
        value = _scalar(raw_value)
        values[full_path] = value
        if raw_value.strip() == "":
            stack.append((indent, key))
            current_list_path = full_path
            current_list_indent = indent
            lists.setdefault(full_path, [])
        else:
            current_list_path = ""
            current_list_indent = -1
            if isinstance(value, list):
                lists[full_path] = value
    return Config(path=path, text=text, values=values, lists=lists)


def is_local_only(config: Config) -> bool:
    return (
        config.bool("execution.local_only", "local_only", default=True)
        or str(config.get("execution.remote_write_policy", "remote_write_policy", default="forbidden")).casefold() == "forbidden"
    )


def publication_target(config: Config) -> str:
    if not config.bool("publishing.enabled", default=False):
        return ""
    value = str(config.get("publishing.target_folder_url", "target_folder_url", default="") or "").strip()
    return "" if value.startswith("<") else value


def publishing_allowed(config: Config) -> bool:
    return bool(publication_target(config))


def validate_config_schema(config: Config, schema_path: Path) -> list[str]:
    """Validate the constrained task YAML against the shipped schema subset."""
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        return [f"无法读取配置Schema：{error}"]
    errors: list[str] = []

    def exists(path: str) -> bool:
        prefix = path + "."
        return path in config.values or path in config.lists or any(key.startswith(prefix) for key in config.values)

    def value_at(path: str):
        if path in config.lists:
            return config.lists[path]
        return config.values.get(path)

    def visit(node: dict, path: str = "") -> None:
        node_type = node.get("type")
        if node_type == "object":
            for key in node.get("required") or []:
                child_path = f"{path}.{key}" if path else key
                if not exists(child_path):
                    errors.append(f"缺少配置：{child_path}")
            for key, child in (node.get("properties") or {}).items():
                child_path = f"{path}.{key}" if path else key
                if exists(child_path):
                    visit(child, child_path)
            return
        value = value_at(path)
        if node_type == "string" and not isinstance(value, str):
            errors.append(f"配置类型错误：{path}应为字符串")
            return
        if node_type == "boolean" and not isinstance(value, bool):
            errors.append(f"配置类型错误：{path}应为布尔值")
            return
        if node_type == "array" and not isinstance(value, list):
            errors.append(f"配置类型错误：{path}应为列表")
            return
        if isinstance(value, str):
            if len(value) < int(node.get("minLength") or 0):
                errors.append(f"配置不能为空：{path}")
            pattern = node.get("pattern")
            if pattern and not re.fullmatch(str(pattern), value):
                errors.append(f"配置格式错误：{path}")
        if "const" in node and value != node["const"]:
            errors.append(f"配置必须为{node['const']!r}：{path}")

    visit(schema)
    return errors
