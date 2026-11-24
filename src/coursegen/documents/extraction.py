"""Нормализованные страницы, заголовки и координаты текстового слоя."""

from dataclasses import dataclass, field
import re
from threading import Lock
import unicodedata

import pymupdf


_pdf_lock = Lock()  # MuPDF access is serialized within each process.


class ExtractionError(ValueError):
    """Короткий диагностический код, пригодный для сохранения в БД."""


@dataclass
class Page:
    number: int
    text: str
    headings: list[tuple[int, int, str]] = field(default_factory=list)
    blocks: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    status: str = "extracted"


def normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def parser_version(fmt: str) -> str:
    return f"pymupdf-{pymupdf.VersionBind}" if fmt == "pdf" else "utf8-nfc-v1"


def layout_hints(text: str) -> list[str]:
    warnings = []
    if re.search(r"\|[^\n]+\||\S\t+\S", text):
        warnings.append("table_layout")
    if re.search(r"[∑∏√∫±]|\$\$|\\(?:frac|sum|sqrt)\b", text):
        warnings.append("formula_layout")
    return warnings


def text_pages(data: bytes, fmt: str, max_pages: int, max_chars: int) -> list[Page]:
    try:
        text = normalize(data.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise ExtractionError("invalid_utf8") from exc
    if re.search(r"[\x00-\x08\x0b\x0e-\x1f]", text):
        raise ExtractionError("binary_text")
    if len(text) > max_chars or text.count("\f") + 1 > max_pages:
        raise ExtractionError("extraction_limit")
    if not text.strip():
        raise ExtractionError("empty_text")
    pages = []
    fence = None
    for number, part in enumerate(text.split("\f"), 1):
        page = Page(number, part, warnings=layout_hints(part))
        pos = 0
        for line in part.splitlines(keepends=True):
            stripped = line.strip()
            marker = re.match(r"^(~{3,}|\x60{3,})", stripped) if fmt == "md" else None
            if marker:
                if fence is None:
                    fence = marker[1]
                elif stripped.rstrip(fence[0]) == "" and len(stripped) >= len(fence):
                    fence = None
                if "code_layout" not in page.warnings:
                    page.warnings.append("code_layout")
            elif fence is None:
                title = re.match(r"^(#{1,6})\s+(.+)$", stripped) if fmt == "md" else None
                numbered = re.match(r"^(\d+(?:\.\d+)*)[.)]?\s+(\S.{0,119})$", stripped)
                if title:
                    page.headings.append((pos, len(title[1]), re.sub(r"\s+#+$", "", title[2])))
                elif numbered:
                    page.headings.append((pos, min(6, numbered[1].count(".") + 1), stripped))
            pos += len(line)
        pages.append(page)
    return pages


def pdf_pages(data: bytes, max_pages: int, max_chars: int) -> list[Page]:
    if not data.startswith(b"%PDF-"):
        raise ExtractionError("invalid_pdf")
    pages = []
    total = 0
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass:
                raise ExtractionError("encrypted_pdf")
            if not 0 < len(doc) <= max_pages:
                raise ExtractionError("page_limit")
            bookmarks: dict[int, list[tuple[int, str]]] = {}
            for level, title, number in doc.get_toc():
                bookmarks.setdefault(number, []).append((level, title))
            for number, source in enumerate(doc, 1):
                blocks, lines, headings = [], [], []
                offset = 0
                for block in source.get_text("blocks", sort=True):
                    if block[6] != 0:
                        continue
                    part = normalize(block[4]).replace("\x00", "\ufffd").strip("\n")
                    if not part.strip():
                        continue
                    if lines:
                        offset += 1
                    end = offset + len(part)
                    if total + end > max_chars:
                        raise ExtractionError("extraction_limit")
                    blocks.append({"bbox": list(block[:4]), "start": offset, "end": end})
                    lines.append(part)
                    if "\n" not in part and re.match(r"^\d+(?:\.\d+)*[.)]?\s+\S", part) and len(part) < 120:
                        headings.append((offset, 1, part))
                    offset = end
                text = "\n".join(lines)
                total += len(text)
                # PDF bookmarks provide page-level section hints. Later bookmarks can
                # point farther down the same page, without a reliable character offset.
                if bookmarks.get(number):
                    level, title = bookmarks[number][0]
                    headings.insert(0, (0, level, title.replace("\x00", "\ufffd")))
                warnings = ["check_pdf_layout", *layout_hints(text)] if text else ["needs_ocr"]
                pages.append(Page(number, text, headings, blocks, warnings,
                                  "extracted" if text else "needs_ocr"))
    except ExtractionError:
        raise
    except (RuntimeError, ValueError, pymupdf.FileDataError) as exc:
        raise ExtractionError("invalid_pdf") from exc
    return pages


def extract_document(data: bytes, fmt: str, *, max_pages: int, max_chars: int) -> list[Page]:
    if fmt == "pdf":
        with _pdf_lock:
            return pdf_pages(data, max_pages, max_chars)
    if fmt in ("txt", "md"):
        return text_pages(data, fmt, max_pages, max_chars)
    raise ExtractionError("unknown_format")
