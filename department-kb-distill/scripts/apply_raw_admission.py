#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import tempfile


ALLOWED_EXTENSIONS = {"adoc", "docx", "pdf", "pptx", "xlsx", "axls", "csv", "md", "txt"}
IMAGE_EXTENSIONS = {"bmp", "gif", "heic", "heif", "ico", "jpeg", "jpg", "png", "svg", "tif", "tiff", "webp"}
AUDIO_EXTENSIONS = {"aac", "flac", "m4a", "mp3", "ogg", "wav", "wma"}
VIDEO_EXTENSIONS = {"avi", "mkv", "mov", "mp4", "mpeg", "mpg", "webm", "wmv"}
ARCHIVE_EXTENSIONS = {"7z", "bz2", "gz", "rar", "tar", "tgz", "zip", "xz"}
EXECUTABLE_EXTENSIONS = {"apk", "app", "dmg", "exe", "msi", "pkg"}
SOURCE_EXTENSIONS = {"html", "htm", "js", "json", "py", "sql", "xml", "yaml", "yml"}


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", delete=False, dir=path.parent) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        temporary = Path(handle.name)
    temporary.replace(path)


def classify(row: dict) -> tuple[bool, str]:
    extension = str(row.get("extension") or "").lower().lstrip(".")
    content_type = str(row.get("content_type") or "").upper()
    if content_type in {"AUDIO"} or extension in AUDIO_EXTENSIONS:
        return False, "音频不进入Raw；需提供文字转写稿或纪要"
    if content_type in {"VIDEO"} or extension in VIDEO_EXTENSIONS:
        return False, "视频不进入Raw；需提供文字说明、纪要或转写稿"
    if content_type in {"ARCHIVE", "ARCHIVE_MEMBER"} or extension in ARCHIVE_EXTENSIONS:
        return False, "压缩包及其成员不直接进入Raw；需解压后按文件逐项提交"
    if extension in IMAGE_EXTENSIONS:
        return False, "独立图片不进入Raw；需整理为含文字说明的Word或钉钉文档"
    if extension in EXECUTABLE_EXTENSIONS:
        return False, "可执行程序或安装包不属于Raw知识材料"
    if extension in SOURCE_EXTENSIONS:
        return False, "网页、程序或配置源文件不在Raw白名单；需整理为说明文档"
    if extension not in ALLOWED_EXTENSIONS:
        return False, f"扩展名.{extension or '未知'}不在Raw格式白名单"
    return True, ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply the Raw v2.0 admission whitelist before extraction.")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--replace-current-snapshot",
        action="store_true",
        help="以当前盘点替换本轮准入快照，不合并历史排除项；供增量维护使用",
    )
    args = parser.parse_args()
    job = args.job.resolve()
    inventory = job / "01-inventory"
    manifest_path = inventory / "raw-manifest.json"
    admission_root = inventory / "raw-admission"
    summary_path = admission_root / "admission-summary.json"
    rows = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise SystemExit("raw-manifest.json 不是数组")
    already_applied = bool(summary_path.exists()) and all(row.get("admission_status") == "已准入" for row in rows)
    if already_applied and not args.force:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        print(f"RAW_ADMISSION_ALREADY_APPLIED total={summary.get('total', 0)} accepted={summary.get('accepted', 0)} excluded={summary.get('excluded', 0)}")
        return
    excluded_path = admission_root / "excluded-manifest.json"
    if args.force and excluded_path.exists() and not args.replace_current_snapshot:
        previous_excluded = json.loads(excluded_path.read_text(encoding="utf-8"))
        by_id = {str(row.get("source_id") or index): row for index, row in enumerate(previous_excluded) if isinstance(row, dict)}
        by_id.update({str(row.get("source_id") or f"active-{index}"): row for index, row in enumerate(rows) if isinstance(row, dict)})
        rows = list(by_id.values())
    snapshot_path = admission_root / "raw-manifest-before-admission.json"
    if not snapshot_path.exists() or args.replace_current_snapshot:
        write_json(snapshot_path, rows)
    accepted: list[dict] = []
    excluded: list[dict] = []
    for original in rows:
        row = dict(original)
        allowed, reason = classify(row)
        row["admission_status"] = "已准入" if allowed else "经Raw标准排除"
        row["admission_reason"] = reason
        if allowed:
            accepted.append(row)
        else:
            row["parse_status"] = "经Raw标准排除"
            row["processing"] = "不进入蒸馏"
            excluded.append(row)
    fields = list(dict.fromkeys([*(rows[0].keys() if rows else []), "admission_status", "admission_reason"]))
    write_json(manifest_path, accepted)
    write_csv(inventory / "raw-manifest.csv", accepted, fields)
    write_json(excluded_path, excluded)
    write_csv(admission_root / "经Raw标准排除清单.csv", excluded, fields)
    summary = {"standard_version": "v2.0", "total": len(rows), "accepted": len(accepted), "excluded": len(excluded), "pending": 0}
    write_json(summary_path, summary)
    report = (
        "# Raw格式准入结果\n\n"
        "- 标准版本：v2.0\n"
        f"- 盘点总数：{len(rows)}\n"
        f"- 已准入：{len(accepted)}\n"
        f"- 经标准排除：{len(excluded)}\n"
        "- 待准入处理：0\n"
        "- 说明：独立图片、音频、视频、压缩包及白名单外格式不进入解析和Codex处理。\n"
    )
    (job / "06-reports").mkdir(parents=True, exist_ok=True)
    (job / "06-reports" / "raw-admission.md").write_text(report, encoding="utf-8")
    print(f"RAW_ADMISSION_OK total={len(rows)} accepted={len(accepted)} excluded={len(excluded)}")


if __name__ == "__main__":
    main()
