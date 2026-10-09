"""Text-layer-first PDF extraction. No model calls, fabricated text, or block evasion.

SPDX-License-Identifier: AGPL-3.0-or-later
"""
import json
import math
import os
import re
import subprocess
import tempfile
import time
import unicodedata
from pathlib import Path

import pymupdf


class PDFError(Exception):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code, self.reason = code, reason


def clean_text(text):
    # Normalize ligatures/Unicode presentation forms, not content or spelling.
    text = unicodedata.normalize("NFKC", text).replace("\x00", "").replace("\r\n", "\n")
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def usable_text(text):
    chars = [c for c in text if not c.isspace()]
    if not chars or not any(c.isalnum() for c in chars):
        return False
    bad = sum(c == "\ufffd" or unicodedata.category(c) in ("Co", "Cc", "Cn") for c in chars)
    return bad / len(chars) < 0.05


def image_coverage(page):
    area = page.rect.get_area()
    if not area:
        return 0
    # Union is unnecessary here: capped sum only selects OCR for sparse layers.
    return min(1.0, sum((pymupdf.Rect(im["bbox"]) & page.rect).get_area()
                        for im in page.get_image_info()) / area)


def needs_ocr(page, text):
    if not usable_text(text):
        return bool(page.get_contents())
    # A running header or page number over a full-page scan is not a usable layer.
    return sum(c.isalnum() for c in text) < 20 and image_coverage(page) > 0.5


def table_markdown(rows):
    if not rows:
        return ""
    width = max(map(len, rows), default=0)
    if width == 0:
        return ""
    def cell(value):
        return clean_text(str(value or "")).replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")
    lines = ["| " + " | ".join(cell(v) for v in row + [None] * (width - len(row))) + " |" for row in rows]
    lines.insert(1, "| " + " | ".join(["---"] * width) + " |")
    return "\n".join(lines)


def page_tables(page, number):
    # Explicit ruling-line detection: do not invent borderless/scanned tables.
    found = page.find_tables(strategy="lines_strict")
    return [{"page": number, "markdown": table_markdown(table.extract())} for table in found.tables
            if table.row_count >= 2 and table.col_count >= 2]


def digital_markdown(page, tables):
    blocks = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)["blocks"]
    headings = []
    sizes = [span["size"] for b in blocks if b["type"] == 0 for line in b["lines"] for span in line["spans"]
             if span["text"].strip()]
    body_size = sorted(sizes)[len(sizes) // 2] if sizes else 12
    # PDF content-stream order keeps columns together on common academic PDFs.
    for block in blocks:
        if block["type"] != 0:
            continue
        lines = [clean_text("".join(s["text"] for s in line["spans"])) for line in block["lines"]]
        text = "\n".join(lines).strip()
        if not text:
            continue
        size = max((s["size"] for line in block["lines"] for s in line["spans"]), default=body_size)
        prefix = "## " if size >= body_size * 1.4 and len(text) < 200 else ""
        headings.append(prefix + text)
    if tables:
        headings.append("### Tables\n\n" + "\n\n".join(t["markdown"] for t in tables))
    return "\n\n".join(headings)


def ocr_text(page, languages, deadline):
    seconds = min(35, deadline - time.monotonic())
    if seconds <= 0:
        raise PDFError("time_limit", "PDF processing time limit reached.")
    # Bound memory even when a malicious PDF declares a huge page size.
    area = max(1, page.rect.width * page.rect.height)
    scale = min(300 / 72, math.sqrt(20_000_000 / area))
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csGRAY, alpha=False)
    with tempfile.TemporaryDirectory(prefix="pdf-ocr-") as temp:
        image = Path(temp) / "page.png"
        pix.save(image)
        try:
            result = subprocess.run(["tesseract", str(image), "stdout", "-l", languages, "--psm", "3"],
                                    capture_output=True, timeout=seconds, env={**os.environ, "OMP_THREAD_LIMIT": "1"})
        except subprocess.TimeoutExpired:
            raise PDFError("ocr_timeout", "OCR page exceeded its processing time limit.") from None
        except OSError:
            raise PDFError("ocr_unavailable", "Tesseract is not available in this build.") from None
    if result.returncode:
        raise PDFError("ocr_failed", "Tesseract could not process this page or the requested language pack is unavailable.")
    return clean_text(result.stdout.decode("utf-8", errors="replace"))


def extract_pdf(path, settings):
    started = time.monotonic()
    deadline = started + settings.get("processingSeconds", 60)
    try:
        doc = pymupdf.open(path, filetype="pdf")
    except Exception:
        raise PDFError("corrupt_pdf", "The PDF is corrupt or cannot be parsed.") from None
    with doc:
        if doc.is_encrypted or (doc.metadata or {}).get("encryption"):
            raise PDFError("encrypted_pdf", "Encrypted PDFs are not opened; provide an unencrypted public PDF.")
        if doc.is_repaired:
            raise PDFError("corrupt_pdf", "The PDF required structural repair; no extraction was charged.")
        if doc.permissions >= 0 and not doc.permissions & pymupdf.PDF_PERM_COPY:
            raise PDFError("permissions_denied", "The PDF does not permit text extraction.")
        if not 1 <= len(doc) <= settings["maxPages"]:
            raise PDFError("page_limit", "PDF page count exceeds maxPages or the document has no pages.")
        pages, tables, markdown_pages = [], [], []
        warnings, total_chars = [], 0
        for number, page in enumerate(doc, 1):
            if time.monotonic() >= deadline:
                raise PDFError("time_limit", "PDF processing time limit reached; no partial PDF is charged.")
            text = clean_text(page.get_text("text"))
            ocr = needs_ocr(page, text)
            if ocr:
                text = ocr_text(page, settings["ocrLanguages"], deadline)
                page_md = text
            else:
                try:
                    current_tables = page_tables(page, number)
                except Exception:
                    current_tables = []
                    warnings.append(f"Table detection unavailable on page {number}.")
                tables.extend(current_tables)
                page_md = digital_markdown(page, current_tables)
            total_chars += len(text)
            if total_chars > 8_000_000:
                raise PDFError("text_limit", "Extracted text exceeds the 8-million-character safety limit.")
            pages.append({"page": number, "text": text, "ocr": ocr})
            markdown_pages.append(f"<!-- Page {number} -->\n\n{page_md}")
        text = "\n\n".join(p["text"] for p in pages)
        if not usable_text(text):
            raise PDFError("no_text", "No usable text was found, including after OCR where needed.")
        meta = doc.metadata or {}
        result = {"status": "extracted", "page_count": len(doc), "text": text,
                  "markdown": "\n\n---\n\n".join(markdown_pages), "pages": pages, "tables": tables,
                  "metadata": {"title": meta.get("title") or None, "author": meta.get("author") or None},
                  "char_count": len(text), "ocr_pages": sum(p["ocr"] for p in pages), "billable": True,
                  "processing_seconds": round(time.monotonic() - started, 3)}
        if warnings:
            result["warnings"] = warnings
        return result


def worker():
    import sys
    path, settings, output = sys.argv[1:]
    settings = json.loads(settings)
    # Keep native parser memory below the 1 GiB Actor container; the adapter remains alive.
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (700 * 1024 * 1024, 700 * 1024 * 1024))
    cpu_seconds = max(1, math.ceil(settings.get("processingSeconds", 60)) + 1)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    try:
        result = extract_pdf(path, settings)
    except PDFError as error:
        result = {"error": error.code, "reason": error.reason}
    except Exception:
        result = {"error": "extraction_failed", "reason": "The PDF could not be completely extracted; no PDF or OCR event is charged."}
    serialized = json.dumps(result, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > 8 * 1024 * 1024:
        serialized = json.dumps({"error": "output_limit", "reason": "Full output exceeds the 8 MiB row safety limit; no truncation or charge."})
    Path(output).write_text(serialized, encoding="utf-8")


if __name__ == "__main__":
    worker()
