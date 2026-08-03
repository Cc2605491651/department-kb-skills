#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


def dependency_python() -> str:
    override = os.environ.get("KB_DISTILL_PYTHON", "").strip()
    if override:
        return override
    candidates = [
        sys.executable,
        str(Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"),
        str(Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3.12"),
    ]
    for candidate in candidates:
        if not Path(candidate).exists():
            continue
        probe = subprocess.run([candidate, "-c", "import charset_normalizer, openpyxl, pdfplumber"], capture_output=True, text=True)
        if probe.returncode == 0:
            return candidate
    raise RuntimeError("未找到包含 charset_normalizer/openpyxl/pdfplumber 的 Python；请设置 KB_DISTILL_PYTHON")


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract admitted source documents with the Skill-bundled adapter.")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=100000)
    parser.add_argument("--retry-failures", action="store_true")
    parser.add_argument("--source-id", action="append", default=[])
    args = parser.parse_args()
    adapter = Path(__file__).resolve().parent / "extract_all.py"
    if not adapter.exists():
        raise RuntimeError(f"Skill内置抽取适配器不存在：{adapter}")
    environment = os.environ.copy()
    environment["KB_DISTILL_JOB"] = str(args.job.resolve())
    command = [dependency_python(), str(adapter), "--workers", str(args.workers), "--limit", str(args.limit)]
    if args.retry_failures:
        command.append("--retry-failures")
    for source_id in args.source_id:
        command.extend(["--source-id", source_id])
    raise SystemExit(subprocess.run(command, env=environment).returncode)


if __name__ == "__main__":
    main()
