#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile
import xml.etree.ElementTree as ET

from charset_normalizer import from_bytes
import openpyxl
import pdfplumber

from config_utils import load_config


CODE_DIR = Path(__file__).resolve().parent
JOB = Path(os.environ.get("KB_DISTILL_JOB") or Path(__file__).resolve().parents[1]).resolve()
INV = JOB / "01-inventory"
CACHE = JOB / "02-extraction-cache"
EXTRACTED = CACHE / "extracted"
ORIGINALS = CACHE / "originals"
META = CACHE / "metadata"
REPORTS = JOB / "06-reports"
MANIFEST_JSON = INV / "raw-manifest.json"
SOFFICE = Path(os.environ.get("KB_DISTILL_SOFFICE") or shutil.which("soffice") or "soffice")

TEXT_EXTS = {"md", "txt", "csv"}
ALLOWED_EXTS = {"adoc", "docx", "pdf", "pptx", "xlsx", "axls", "csv", "md", "txt"}
MANIFEST_HEADERS = [
    "source_id", "department", "source_path", "file_name", "node_id", "source_url",
    "node_type", "content_type", "extension", "create_time", "update_time",
    "creator_uid", "creator_name", "owner", "permission_snapshot", "snapshot_status",
    "virtual_kind", "parent_source_id", "parent_node_id", "resource_id",
    "parse_status", "first_attempt", "second_attempt", "last_error",
    "source_hash", "extracted_hash", "extracted_chars", "raw_mirror_path", "business_path",
    "raw_page_url", "business_page_url", "processing", "status",
    "delivery_status", "delivery_error",
    "failure_stage", "failure_category", "attempt_count", "last_attempt_at",
    "download_bytes", "file_signature", "http_status",
]

write_lock = threading.Lock()


def config_value(key: str, default: str = "") -> str:
    config = load_config(JOB)
    aliases = {
        "id_prefix": ("source.stable_id_prefix", "stable_id_prefix", "id_prefix"),
        "task_id": ("task_id",),
        "batch_id": ("batch_id",),
        "source_snapshot_at": ("source.snapshot_at", "source_snapshot_at"),
    }
    return str(config.get(*aliases.get(key, (key,)), default=default) or default)


TASK_ID = config_value("task_id", "knowledge-base-distill")
BATCH_ID = config_value("batch_id", "local-batch")
SOURCE_SNAPSHOT_AT = config_value("source_snapshot_at", "")


class DwsError(RuntimeError):
    pass


class ParseIncomplete(RuntimeError):
    pass


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg", "template", "canvas"}:
            self.skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg", "template", "canvas"} and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip_depth and data.strip():
            self.parts.append(data)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if isinstance(data, bytes):
        tmp.write_bytes(data)
    else:
        tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


def parse_json_output(stdout: str) -> dict:
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start < 0 or end < start:
        raise DwsError("dws 未返回可解析 JSON")
    return json.loads(stdout[start:end + 1])


def dws_failure_message(result: subprocess.CompletedProcess) -> str:
    for output in (result.stdout, result.stderr, (result.stdout or "") + "\n" + (result.stderr or "")):
        start, end = output.find("{"), output.rfind("}")
        if start < 0 or end < start:
            continue
        try:
            payload = json.loads(output[start:end + 1])
        except json.JSONDecodeError:
            continue
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            category = str(error.get("category") or "error")
            message = str(error.get("message") or error.get("reason") or "命令失败")
            return f"DWS {category}: {message}"
    return "dws 命令执行失败"


def run_dws(args: list[str], timeout: int = 360) -> dict:
    command = ["dws", *args, "--format", "json"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        first_message = dws_failure_message(result)
        if "未登录" in first_message or "auth" in first_message.lower():
            raise DwsError(first_message)
        result = subprocess.run([*command, "--verbose"], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise DwsError(dws_failure_message(result))
    payload = parse_json_output(result.stdout)
    if payload.get("success") is False:
        raise DwsError("dws 返回 success=false")
    return payload


def safe_error(error: BaseException) -> str:
    name = type(error).__name__
    message = re.sub(r"https?://\S+", "[URL]", str(error))
    message = re.sub(r"(?i)(password|token|secret|key)\s*[:=]\s*\S+", r"\1=[REDACTED]", message)
    return f"{name}: {message[:300]}"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def find_download_url(value: object) -> str:
    """Find a DWS-issued pre-signed download URL without logging it."""
    if isinstance(value, str) and value.startswith(("https://", "http://")):
        return value
    if isinstance(value, dict):
        for key in ("resourceUrl", "downloadUrl", "url", "fileUrl"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith(("https://", "http://")):
                return candidate
        for nested in value.values():
            candidate = find_download_url(nested)
            if candidate:
                return candidate
    if isinstance(value, list):
        for nested in value:
            candidate = find_download_url(nested)
            if candidate:
                return candidate
    return ""


def file_signature(path: Path) -> str:
    data = path.read_bytes()[:16]
    return data.hex().upper()


def validate_download(path: Path, extension: str) -> tuple[int, str]:
    if not path.exists():
        raise DwsError("下载完成后目标文件不存在")
    size = path.stat().st_size
    if size <= 0:
        raise DwsError("下载结果为空文件")
    head = path.read_bytes()[:16]
    ext = extension.lower().lstrip(".")
    expected = {
        "pdf": (b"%PDF-",),
        "pptx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
        "pptm": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
        "docx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
        "docm": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
        "xlsx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
        "xlsm": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
        "zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
        "png": (b"\x89PNG\r\n\x1a\n",),
        "jpg": (b"\xff\xd8\xff",),
        "jpeg": (b"\xff\xd8\xff",),
        "gif": (b"GIF87a", b"GIF89a"),
    }
    signatures = expected.get(ext)
    if signatures and not any(head.startswith(signature) for signature in signatures):
        raise DwsError(f"下载内容头与 .{ext} 不匹配（signature={head.hex().upper()}）")
    return size, head.hex().upper()


def http_download(url: str, output: Path, headers: dict | None = None, timeout: int = 300) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    request_headers = {str(k): str(v) for k, v in (headers or {}).items()}
    request_headers.setdefault("User-Agent", "department-kb-distill/1.0")
    request_headers.setdefault("Connection", "close")
    request = Request(url, headers=request_headers, method="GET")
    context = ssl.create_default_context()
    try:
        with urlopen(request, timeout=timeout, context=context) as response:
            status = int(getattr(response, "status", 200) or 200)
            if status < 200 or status >= 300:
                raise DwsError(f"下载 HTTP 状态异常：{status}")
            with output.open("wb") as handle:
                shutil.copyfileobj(response, handle, length=1024 * 1024)
            return status
    except HTTPError as error:
        raise DwsError(f"下载 HTTP 状态异常：{error.code}") from error
    except URLError as error:
        # Some DingTalk dual-stack OSS endpoints intermittently terminate the
        # TLS stream early in urllib on macOS. Curl's HTTP/1.1 + IPv4 transport
        # is a compatibility fallback for the DWS-issued pre-signed URL only.
        curl = shutil.which("curl")
        if not curl:
            raise DwsError(f"下载网络异常：{safe_error(error)}") from error
        output.unlink(missing_ok=True)
        result = subprocess.run([
            curl, "--fail", "--location", "--silent", "--show-error",
            "--retry", "2", "--retry-all-errors", "--http1.1", "--ipv4",
            "--output", str(output), "--write-out", "%{http_code}", "--url", url,
        ], capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            output.unlink(missing_ok=True)
            raise DwsError(f"下载网络异常：{safe_error(error)}；兼容传输仍失败：curl exit={result.returncode}") from error
        try:
            return int(result.stdout.strip()[-3:])
        except ValueError as status_error:
            raise DwsError("兼容传输成功但未返回 HTTP 状态") from status_error


def download_from_payload(payload: dict, output: Path, extension: str) -> tuple[int, int, str]:
    local_path = payload.get("localPath") or payload.get("output") or payload.get("filePath")
    if local_path and Path(local_path).exists():
        shutil.copy2(Path(local_path), output)
        size, signature = validate_download(output, extension)
        return 200, size, signature
    url = find_download_url(payload)
    if not url:
        raise DwsError("DWS 未返回本地文件或可用下载地址")
    status = http_download(url, output, payload.get("headers") if isinstance(payload.get("headers"), dict) else None)
    size, signature = validate_download(output, extension)
    return status, size, signature


def decode_text(data: bytes, strict: bool = True) -> str:
    if not data:
        return ""
    match = from_bytes(data).best()
    if match is None:
        if strict:
            raise UnicodeError("无法识别文本编码")
        return data.decode("utf-8", errors="replace")
    text = str(match)
    if strict and "\x00" in text[:1000]:
        raise UnicodeError("疑似二进制内容")
    return text


def extract_html(data: bytes) -> str:
    decoded = decode_text(data)
    parser = TextExtractor()
    parser.feed(decoded)
    return "\n".join(parser.parts)


def xml_part_text(zf: zipfile.ZipFile, name: str, text_tags: set[str]) -> str:
    root = ET.fromstring(zf.read(name))
    lines: list[str] = []
    for paragraph in root.iter():
        if paragraph.tag.endswith("}p"):
            parts = [node.text or "" for node in paragraph.iter() if node.tag.split("}")[-1] in text_tags]
            if parts:
                lines.append("".join(parts))
    if not lines:
        lines = [node.text or "" for node in root.iter() if node.tag.split("}")[-1] in text_tags]
    return "\n".join(line for line in lines if line.strip())


def extract_docx(path: Path) -> str:
    sections: list[str] = []
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        wanted = ["word/document.xml"]
        wanted += sorted(n for n in names if re.fullmatch(r"word/header\d+\.xml", n))
        wanted += sorted(n for n in names if re.fullmatch(r"word/footer\d+\.xml", n))
        wanted += [n for n in ["word/footnotes.xml", "word/endnotes.xml", "word/comments.xml"] if n in names]
        for name in wanted:
            sections.append(f"[[{name}]]\n{xml_part_text(zf, name, {'t', 'delText', 'instrText'})}")
    return "\n\n".join(sections)


def extract_pptx(path: Path) -> str:
    sections: list[str] = []
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=lambda n: int(re.search(r"(\d+)", Path(n).stem).group(1)))
        notes = sorted(n for n in names if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", n))
        comments = sorted(n for n in names if re.search(r"ppt/comments/.+\.xml$", n))
        for name in [*slides, *notes, *comments]:
            sections.append(f"[[{name}]]\n{xml_part_text(zf, name, {'t'})}")
    return "\n\n".join(sections)


def extract_xlsx_openpyxl(path: Path) -> str:
    wb = openpyxl.load_workbook(path, read_only=False, data_only=False, keep_links=True)
    sections: list[str] = []
    if wb.defined_names:
        sections.append("[[DEFINED_NAMES]]\n" + "\n".join(str(item) for item in wb.defined_names.values()))
    for ws in wb.worksheets:
        lines = [f"[[SHEET {ws.title} state={ws.sheet_state}]]"]
        if ws.merged_cells.ranges:
            lines.append("MERGED=" + ",".join(str(x) for x in ws.merged_cells.ranges))
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is not None:
                    lines.append(f"{cell.coordinate}\t{cell.value}")
                if cell.comment is not None:
                    lines.append(f"{cell.coordinate}\t[[COMMENT author={cell.comment.author}]]\t{cell.comment.text}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def extract_xlsx_ooxml(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root:
                shared.append("".join(node.text or "" for node in si.iter() if node.tag.endswith("}t")))
        sections: list[str] = []
        for name in sorted(n for n in zf.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)):
            root = ET.fromstring(zf.read(name))
            lines = [f"[[{name}]]"]
            for cell in (node for node in root.iter() if node.tag.endswith("}c")):
                ref = cell.attrib.get("r", "")
                kind = cell.attrib.get("t", "")
                formula = next((n.text for n in cell if n.tag.endswith("}f")), None)
                value = next((n.text for n in cell if n.tag.endswith("}v")), "")
                if kind == "s" and value and int(value) < len(shared):
                    value = shared[int(value)]
                lines.append(f"{ref}\t{('=' + formula) if formula is not None else value}")
            sections.append("\n".join(lines))
        return "\n\n".join(sections)


def extract_pdf(path: Path, work: Path) -> tuple[str, bool]:
    pages: list[str] = []
    with pdfplumber.open(path) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            tables = page.extract_tables() or []
            table_text = "\n".join("\t".join("" if c is None else str(c) for c in row) for table in tables for row in table)
            combined = text + (("\n[[TABLES]]\n" + table_text) if table_text else "")
            pages.append(combined)
    rendered = "\n\n".join(f"[[PAGE {i}]]\n{text}" for i, text in enumerate(pages, start=1))
    if len(re.sub(r"\s+|\[\[PAGE \d+\]\]", "", rendered)) < 50:
        raise ParseIncomplete("扫描型或无可检索文字PDF不符合Raw准入标准；请提供可检索PDF或文字稿")
    return rendered, False


def libreoffice_to_text(path: Path, work: Path) -> str:
    profile = work / "lo-profile"
    profile.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([
        str(SOFFICE), "--headless", f"-env:UserInstallation=file://{profile}",
        "--convert-to", "txt:Text", "--outdir", str(work), str(path),
    ], capture_output=True, text=True, timeout=600)
    output = work / f"{path.stem}.txt"
    if result.returncode != 0 or not output.exists():
        raise RuntimeError("LibreOffice 文本转换失败")
    return decode_text(output.read_bytes(), strict=False)


def pptx_via_pdf(path: Path, work: Path) -> str:
    profile = work / "lo-profile"
    profile.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([
        str(SOFFICE), "--headless", f"-env:UserInstallation=file://{profile}",
        "--convert-to", "pdf", "--outdir", str(work), str(path),
    ], capture_output=True, text=True, timeout=600)
    pdf = work / f"{path.stem}.pdf"
    if result.returncode != 0 or not pdf.exists():
        raise RuntimeError("PPT 转 PDF 失败")
    return extract_pdf(pdf, work / "ppt-pdf-pages")[0]


def parse_local_file(path: Path, extension: str, work: Path) -> tuple[str, str, str, str]:
    ext = extension.lower().lstrip(".")
    second = ""
    try:
        if ext in TEXT_EXTS:
            text = decode_text(path.read_bytes())
            return text, "字符集识别后全文读取", second, "text_decode"
        if ext in {"html", "htm"}:
            text = extract_html(path.read_bytes())
            return text, "HTMLParser 提取可见全文", second, "html_parser"
        if ext == "pdf":
            text, _ = extract_pdf(path, work / "pdf-pages")
            return text, "pdfplumber 全页可检索文本与表格提取", "", "pdfplumber"
        if ext in {"docx", "docm"}:
            return extract_docx(path), "OOXML 正文/表格/批注/页眉页脚/脚注尾注", second, "docx_ooxml"
        if ext in {"pptx", "pptm"}:
            text = extract_pptx(path)
            if len(re.sub(r"\s+|\[\[[^]]+\]\]", "", text)) < 20:
                raise ParseIncomplete("纯图片或无可提取文字PPT不符合Raw准入标准；请补充文字稿")
            return text, "OOXML 幻灯片/备注/批注全文", second, "pptx_ooxml"
        if ext in {"xlsx", "xlsm"}:
            return extract_xlsx_openpyxl(path), "openpyxl 全工作表/单元格/公式/批注", second, "xlsx_openpyxl"
        if ext in {"woff", "woff2", "ttf", "otf"}:
            from fontTools.ttLib import TTFont
            font = TTFont(path)
            names = []
            for record in font["name"].names:
                try:
                    value = record.toUnicode()
                except Exception:
                    value = record.string.decode("utf-8", errors="replace")
                names.append(f"nameID={record.nameID}\t{value}")
            return "\n".join(names), "fontTools 读取字体名称与元数据", second, "fonttools"
        if ext == "bin":
            result = subprocess.run(["strings", "-a", str(path)], capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                raise RuntimeError("strings 读取失败")
            return result.stdout, "strings 提取可打印内容；原二进制附件完整保留", second, "binary_strings"
        raise RuntimeError(f"暂不支持扩展名 {ext}")
    except Exception as first_error:
        first = safe_error(first_error)
        if ext in TEXT_EXTS or ext in {"html", "htm"}:
            return decode_text(path.read_bytes(), strict=False), first, "UTF-8 replacement 全量读取", "text_fallback"
        if ext in {"docx", "docm"}:
            return libreoffice_to_text(path, work), first, "LibreOffice 转纯文本", "docx_libreoffice"
        if ext in {"pptx", "pptm"}:
            return pptx_via_pdf(path, work), first, "LibreOffice 转 PDF 后全页提取/OCR", "pptx_pdf"
        if ext in {"xlsx", "xlsm"}:
            return extract_xlsx_ooxml(path), first, "OOXML 兼容读取全部工作表单元格与公式", "xlsx_ooxml"
        if ext == "pdf":
            text, _ = extract_pdf(path, work / "pdf-retry")
            return text, first, "pdfplumber兼容重试", "pdfplumber_retry"
        if ext in {"woff", "woff2", "ttf", "otf", "bin"}:
            result = subprocess.run(["file", "-b", str(path)], capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                raise first_error
            return result.stdout.rstrip("\n"), first, "file 类型识别；原二进制附件完整保留", "binary_file_metadata"
        raise first_error


def download_node(row: dict, output: Path) -> tuple[str, str, int, int, int, str]:
    errors: list[str] = []
    for attempt in range(1, 4):
        output.unlink(missing_ok=True)
        try:
            payload = run_dws(["doc", "download", "--node", row["node_id"], "--output", str(output)])
            status, size, signature = download_from_payload(payload, output, row.get("extension", "bin"))
            return "dws doc download 获取凭证并原样下载", "；".join(errors), attempt, status, size, signature
        except Exception as error:
            errors.append(f"第{attempt}次下载：{safe_error(error)}")
            if attempt < 3:
                time.sleep(attempt)
    raise DwsError("；".join(errors))


def base_terminal_row(row: dict) -> dict:
    return {header: row.get(header, "") for header in MANIFEST_HEADERS}


def classify_failure(error: BaseException, stage: str) -> str:
    message = str(error).lower()
    if "未登录" in message or "not_authenticated" in message or "dws auth" in message:
        return "DWS未登录"
    if "权限" in message or "permission" in message or "forbidden" in message:
        return "权限不足"
    if "签名" in message or "signature" in message or "过期" in message:
        return "下载凭证异常"
    if "http 状态" in message or "网络" in message or "ssl" in message or "timed out" in message:
        return "下载网络异常"
    if "密码" in message or "encrypted" in message:
        return "文件加密"
    if "badzipfile" in message or "not a zip" in message or "损坏" in message:
        return "文件损坏或格式不符"
    if "不支持扩展名" in message:
        return "暂不支持格式"
    if stage == "download":
        return "下载失败"
    if stage == "parse":
        return "解析失败"
    return "处理失败"


def persist_success(row: dict, original: Path | None, text: str, method: str, first: str, second: str) -> dict:
    text_bytes = text.encode("utf-8")
    original_bytes = original.read_bytes() if original and original.exists() else text_bytes
    row.update({
        "first_attempt": first,
        "second_attempt": second,
        "source_hash": sha256_bytes(original_bytes),
        "extracted_hash": sha256_bytes(text_bytes),
        "extracted_chars": len(text),
        "parse_status": "全文已解析",
        "processing": "已索引",
        "status": "候选",
        "last_error": "",
        "failure_stage": "",
        "failure_category": "",
        "last_attempt_at": now_iso(),
    })
    atomic_write(EXTRACTED / f"{row['source_id']}.txt", text)
    if original and original.exists():
        destination = ORIGINALS / f"{row['source_id']}.{row.get('extension') or 'bin'}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, destination)
    atomic_write(META / f"{row['source_id']}.json", json.dumps({
        "source_id": row["source_id"],
        "source_path": row["source_path"],
        "method": method,
        "source_hash": row["source_hash"],
        "extracted_hash": row["extracted_hash"],
        "extracted_chars": row["extracted_chars"],
        "download_bytes": row.get("download_bytes", ""),
        "file_signature": row.get("file_signature", ""),
        "http_status": row.get("http_status", ""),
    }, ensure_ascii=False, indent=2) + "\n")
    return row


def process_row(row: dict) -> tuple[dict, list[dict]]:
    row = base_terminal_row(row)
    children: list[dict] = []
    current_stage = "prepare"
    with tempfile.TemporaryDirectory(prefix=f"distill-{row['source_id']}-") as tmp:
        work = Path(tmp)
        ext = (row.get("extension") or "bin").lower().lstrip(".")
        try:
            if ext not in ALLOWED_EXTS and ext not in {"aexcel"}:
                raise RuntimeError(f"Raw准入门禁阻断扩展名 .{ext}；独立图片、音频、视频、压缩包和白名单外格式禁止进入抽取")
            if row.get("content_type") == "ALIDOC" and ext in {"axls", "aexcel"}:
                current_stage = "export_online_sheet"
                exported = work / "online-sheet.xlsx"
                payload = run_dws(["sheet", "export", "--node", row["node_id"], "--output", str(exported)], timeout=900)
                attempts = 1
                http_status = 200
                if not exported.exists():
                    http_status, download_bytes, signature = download_from_payload(payload, exported, "xlsx")
                else:
                    download_bytes, signature = validate_download(exported, "xlsx")
                row.update({
                    "attempt_count": attempts, "http_status": http_status, "download_bytes": download_bytes,
                    "file_signature": signature, "last_attempt_at": now_iso(),
                })
                current_stage = "parse"
                text, first, second, method = parse_local_file(exported, "xlsx", work / "parse-sheet")
                row = persist_success(
                    row, exported, text, method,
                    f"dws sheet export 只读导出 XLSX；{first}", second,
                )
                return row, children

            if row.get("content_type") == "ALIDOC" and ext == "adoc":
                current_stage = "read_online_document"
                try:
                    payload = run_dws(["doc", "read", "--node", row["node_id"]])
                    text = payload.get("markdown")
                    if text is None:
                        raise ParseIncomplete("markdown 字段缺失")
                    first, second, method = "dws doc read（Markdown 全文）", "", "dws_markdown"
                except Exception as first_error:
                    exported = work / "fallback.docx"
                    run_dws(["doc", "export", "--node", row["node_id"], "--output", str(exported)], timeout=420)
                    text = extract_docx(exported)
                    first, second, method = safe_error(first_error), "dws doc export→DOCX OOXML 全文", "dws_export_docx"
                row = persist_success(row, None, text, method, first, second)
                return row, children

            local = work / f"original.{ext}"
            current_stage = "download"
            row["first_attempt"] = "dws doc download 获取临时凭证并原样下载"
            row["second_attempt"] = "每次刷新凭证重试 3 次"
            download_attempt, download_second, attempts, http_status, download_bytes, signature = download_node(row, local)
            row.update({
                "attempt_count": attempts,
                "http_status": http_status,
                "download_bytes": download_bytes,
                "file_signature": signature,
                "last_attempt_at": now_iso(),
            })
            current_stage = "parse"
            text, first, second, method = parse_local_file(local, ext, work / "parse")
            combined_second = "；".join(part for part in [download_second, second] if part)
            row = persist_success(row, local, text, method, f"{download_attempt}；{first}", combined_second)
            return row, children
        except Exception as error:
            if current_stage == "download" and not row.get("attempt_count"):
                row["attempt_count"] = 3
            row.update({
                "parse_status": "失败已登记",
                "first_attempt": row.get("first_attempt") or "按文件类型全文解析",
                "second_attempt": row.get("second_attempt") or "更换兼容方式重试一次",
                "last_error": safe_error(error),
                "processing": "阻塞",
                "status": "阻塞",
                "failure_stage": current_stage,
                "failure_category": classify_failure(error, current_stage),
                "last_attempt_at": now_iso(),
            })
            return row, children


def save_state(rows: list[dict], current: int = 0, batch_total: int = 0) -> None:
    rows = [base_terminal_row(row) for row in rows]
    rows.sort(key=lambda row: row.get("source_path", ""))
    atomic_write(MANIFEST_JSON, json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", delete=False, dir=INV) as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_HEADERS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        temp_name = handle.name
    Path(temp_name).replace(INV / "raw-manifest.csv")

    failures = [row for row in rows if row.get("parse_status") == "失败已登记"]
    failure_lines = [
        "source_id,department,source_path,file_type,failure_stage,failure_category,attempt_count,"
        "download_bytes,file_signature,http_status,first_attempt,second_attempt,last_error,recommended_action"
    ]
    for row in failures:
        category = row.get("failure_category", "")
        recommendation = {
            "权限不足": "由源文档管理员补充读取权限后重试",
            "下载凭证异常": "刷新下载凭证后重试，禁止复用过期地址",
            "下载网络异常": "重新获取凭证并重试网络下载",
            "文件加密": "由业务负责人提供未加密副本或确认仅按附件归档",
            "文件损坏或格式不符": "由业务负责人重新上传可正常打开的源文件",
            "暂不支持格式": "保留原附件并登记后续转换策略",
            "解析失败": "保留原件，改用兼容解析器或人工核验",
            "DWS未登录": "执行 dws auth login 恢复只读访问后，仅重试该文件",
        }.get(category, "人工检查源文件、权限、密码或损坏情况")
        values = [
            row["source_id"], row["department"], row["source_path"], row.get("extension", ""),
            row.get("failure_stage", ""), category, row.get("attempt_count", ""),
            row.get("download_bytes", ""), row.get("file_signature", ""), row.get("http_status", ""),
            row.get("first_attempt", ""), row.get("second_attempt", ""), row.get("last_error", ""), recommendation,
        ]
        failure_lines.append(",".join(csv_escape(value) for value in values))
    atomic_write(INV / "parse-failure-list.csv", "\n".join(failure_lines) + "\n")

    indexed = sum(row.get("parse_status") == "全文已解析" for row in rows)
    pending = sum(row.get("parse_status") == "待解析" for row in rows)
    silent = len(rows) - (indexed + len(failures) + pending)
    formula = f"{len(rows)} = {indexed} + {len(failures)}" if pending == 0 else "待全部解析完成后闭合"
    progress = f"# 批次进度\n\n- task_id: {TASK_ID}\n- batch_id: {BATCH_ID}\n- status: {'全文解析已完成' if pending == 0 else '正在全文解析'}\n- source_snapshot_at: {SOURCE_SNAPSHOT_AT}\n- 当前批次进度: {current}/{batch_total}\n- Raw准入总数: {len(rows)}\n- 已完成全文解析: {indexed}\n- 重试后仍解析失败数: {len(failures)}\n- 待解析: {pending}\n- 蒸馏成功数: {indexed}\n- 静默遗漏: {silent}\n- 总量公式: {formula}\n"
    atomic_write(REPORTS / "batch-progress.md", progress)
    atomic_write(REPORTS / "exception-report.md", f"# 异常报告\n\n- 重试后解析失败：{len(failures)}\n- 待解析：{pending}\n\nRaw拒绝格式见准入排除清单；解析失败详见本地解析失败清单。\n")


def csv_escape(value: object) -> str:
    text = "" if value is None else str(value)
    if any(char in text for char in [",", "\"", "\n", "\r"]):
        return '"' + text.replace('"', '""') + '"'
    return text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--retry-failures", action="store_true")
    parser.add_argument("--source-id", action="append", default=[])
    args = parser.parse_args()
    for directory in [EXTRACTED, ORIGINALS, META, REPORTS]:
        directory.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
    if args.retry_failures:
        selected_ids = set(args.source_id)
        failed_archive_parents = {
            row.get("parent_source_id")
            for row in rows
            if row.get("parse_status") == "失败已登记" and row.get("virtual_kind") == "archive_member"
            and (not selected_ids or row.get("source_id") in selected_ids)
        }
        for row in rows:
            should_reset = (
                (row.get("parse_status") == "失败已登记" and row.get("virtual_kind") != "archive_member" and (not selected_ids or row.get("source_id") in selected_ids))
                or row.get("source_id") in failed_archive_parents
            )
            if should_reset:
                row.update({
                    "parse_status": "待解析",
                    "first_attempt": "",
                    "second_attempt": "",
                    "last_error": "",
                    "processing": "待处理",
                    "status": "候选",
                    "failure_stage": "",
                    "failure_category": "",
                    "attempt_count": "",
                    "last_attempt_at": "",
                    "download_bytes": "",
                    "file_signature": "",
                    "http_status": "",
                })
    by_id = {row["source_id"]: row for row in rows}
    selected_ids = set(args.source_id)
    pending = [row for row in rows if row.get("parse_status") == "待解析" and (not selected_ids or row.get("source_id") in selected_ids)]
    pending = pending[:args.limit]
    save_state(rows, 0, len(pending))
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_row, row): row["source_id"] for row in pending}
        for future in concurrent.futures.as_completed(futures):
            result, children = future.result()
            by_id[result["source_id"]] = result
            for child in children:
                by_id[child["source_id"]] = child
            rows = list(by_id.values())
            completed += 1
            if completed % 5 == 0 or completed == len(pending):
                with write_lock:
                    save_state(rows, completed, len(pending))
            if completed % 25 == 0 or completed == len(pending):
                indexed = sum(row.get("parse_status") == "全文已解析" for row in rows)
                failed = sum(row.get("parse_status") == "失败已登记" for row in rows)
                print(f"已处理 {completed}/{len(pending)}；Raw={len(rows)} 已解析={indexed} 失败={failed}", flush=True)
    print(f"EXTRACT_BATCH_OK processed={completed} raw_total={len(rows)}", flush=True)


if __name__ == "__main__":
    main()
