#!/usr/bin/env python3
"""Install both department knowledge-base skills by symlink or copy."""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path


SKILL_NAMES = ("department-kb-distill", "department-kb-maintain")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="安装部门知识库全量蒸馏和增量维护Skill")
    parser.add_argument(
        "--target",
        required=True,
        help="Agent的个人Skill目录，例如 ~/.agents/skills",
    )
    parser.add_argument(
        "--mode",
        choices=("link", "copy"),
        default="link",
        help="link便于git pull后立即生效；copy用于不支持软链接的环境",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="目标已存在时先备份旧版本，再安装当前版本",
    )
    return parser.parse_args()


def backup_existing(destination: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = destination.with_name(f"{destination.name}.backup-{timestamp}")
    counter = 1
    while backup.exists() or backup.is_symlink():
        backup = destination.with_name(f"{destination.name}.backup-{timestamp}-{counter}")
        counter += 1
    destination.rename(backup)
    return backup


def install_skill(source: Path, destination: Path, mode: str, replace: bool) -> str:
    if not (source / "SKILL.md").is_file():
        raise RuntimeError(f"缺少SKILL.md：{source}")

    if destination.is_symlink() and destination.resolve() == source.resolve() and mode == "link":
        return f"已连接，无需重复安装：{destination}"

    backup: Path | None = None
    if destination.exists() or destination.is_symlink():
        if not replace:
            raise RuntimeError(f"目标已存在：{destination}；如需替换请增加 --replace")
        backup = backup_existing(destination)

    if mode == "link":
        destination.symlink_to(source, target_is_directory=True)
    else:
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns(".DS_Store", "__pycache__", "*.pyc"),
        )

    message = f"安装成功：{destination}"
    if backup:
        message += f"；旧版本备份：{backup}"
    return message


def main() -> int:
    args = parse_args()
    repository = Path(__file__).resolve().parent
    target = Path(args.target).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)

    for name in SKILL_NAMES:
        print(install_skill(repository / name, target / name, args.mode, args.replace))

    print("两个Skill已安装。若Agent没有立即识别，请重启Agent。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
