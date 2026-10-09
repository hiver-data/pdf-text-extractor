"""Developer tests; fixture documents are generated, not claimed as board bench data."""
import asyncio
import io
import json
import time
from types import SimpleNamespace

import httpx
import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFont

from src.extractor import PDFError, clean_text, extract_pdf, table_markdown, usable_text
from src.main import persist
from src.network import FetchError, Fetcher, diagnostic, extract_url, options, public_dns, public_url


@pytest.fixture
def opts():
    return options({"urls": ["https://example.com/file.pdf"]})


def document(path, *, scan=False, pages=1):
    with pymupdf.open() as doc:
        for i in range(pages):
            page = doc.new_page(width=612, height=792)
            if scan and i == pages - 1:
                image = Image.new("RGB", (1530, 1980), "white")
                draw = ImageDraw.Draw(image)
                font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 45)
                draw.text((150, 220), "POWERLIFTING GUIDEPOSTS", fill="black", font=font)
                draw.text((150, 310), "A scanned business letter.", fill="black", font=font)
                buf = io.BytesIO()
                image.save(buf, format="PNG")
                page.insert_image(page.rect, stream=buf.getvalue())
            else:
                page.insert_text((72, 90), f"Example report page {i+1}. Text layer should not need OCR.")
        doc.set_metadata({"title": "Example report", "author": "Developer fixture"})
        doc.save(path)
    return path


def test_text_layer_metadata_and_full_text(tmp_path, opts, monkeypatch):
    path = document(tmp_path / "digital.pdf", pages=2)
    monkeypatch.setattr("src.extractor.ocr_text", lambda *a: pytest.fail("OCR called for usable layer"))
    row = extract_pdf(path, opts)
    assert row["status"] == "extracted"
    assert row["page_count"] == 2 and row["ocr_pages"] == 0
    assert row["char_count"] == len(row["text"])
    assert all(not p["ocr"] for p in row["pages"])
    assert "page 2" in row["text"] and "<!-- Page 2 -->" in row["markdown"]
    assert row["metadata"] == {"title": "Example report", "author": "Developer fixture"}


def test_real_tesseract_mixed_pdf(tmp_path, opts):
    path = document(tmp_path / "mixed.pdf", scan=True, pages=2)
    row = extract_pdf(path, opts)
    assert row["ocr_pages"] == 1 and row["pages"][0]["ocr"] is False
    assert row["pages"][1]["ocr"] is True
    assert "POWERLIFTING GUIDEPOSTS" in row["text"]
    assert "scanned business letter" in row["text"]


def test_ruled_table_markdown(tmp_path, opts):
    path = tmp_path / "table.pdf"
    with pymupdf.open() as doc:
        p = doc.new_page()
        p.insert_text((50, 60), "Report with table")
        for y in [100, 130, 160]:
            p.draw_line((50, y), (300, y))
        for x in [50, 175, 300]:
            p.draw_line((x, 100), (x, 160))
        for x, y, text in [(60, 120, "Company"), (185, 120, "Count"), (60, 150, "Example"), (185, 150, "42")]:
            p.insert_text((x, y), text)
        doc.save(path)
    row = extract_pdf(path, opts)
    assert row["tables"] and row["tables"][0]["page"] == 1
    assert "| Example | 42 |" in row["tables"][0]["markdown"]
    assert row["tables"][0]["markdown"] in row["markdown"]


def test_blank_doc_is_not_fake_success(tmp_path, opts):
    path = tmp_path / "blank.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(path)
    with pytest.raises(PDFError, match="No usable text"):
        extract_pdf(path, opts)


def test_encrypted_diagnostic(tmp_path, opts):
    path = tmp_path / "encrypted.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), "Protected text")
        doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="fixture-only", owner_pw="fixture-owner")
    with pytest.raises(PDFError) as e:
        extract_pdf(path, opts)
    assert e.value.code == "encrypted_pdf"


def test_corrupt_doc(tmp_path, opts):
    path = tmp_path / "corrupt.pdf"
    path.write_bytes(b"%PDF-1.7\nmalformed")
    with pytest.raises(PDFError) as e:
        extract_pdf(path, opts)
    assert e.value.code == "corrupt_pdf"


def test_page_limit_no_truncation(tmp_path, opts):
    path = document(tmp_path / "digital.pdf", pages=2)
    with pytest.raises(PDFError) as e:
        extract_pdf(path, {**opts, "maxPages": 1})
    assert e.value.code == "page_limit"


@pytest.mark.parametrize("raw", [{}, {"urls": "https://example.com/a.pdf"}, {"urls": []}, {"urls": ["x"], "maxPages": True},
                                    {"urls": ["x"], "ocrLanguages": "eng;false"}, {"urls": ["x"], "ocrLanguages": "zzz"},
                                    {"urls": ["x"], "pdfTimeoutSeconds": 0}, {"urls": ["x"] * 101}])
def test_input_validation(raw):
    with pytest.raises(FetchError):
        options(raw)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1/a.pdf", "http://10.0.0.1/a.pdf", "http://[::1]/a.pdf",
                                    "http://localhost/a.pdf", "https://x.local/a.pdf", "https://user:pass@example.com/a.pdf",
                                    "http://example.com:3110/a.pdf", "https://example.com/a b.pdf", None])
def test_url_validation(url):
    with pytest.raises(FetchError):
        public_url(url)


def test_public_url_no_identifier_repair():
    assert public_url("https://example.com/a.pdf?x=1#page=2") == "https://example.com/a.pdf?x=1"
    assert clean_text("o\ufb03ce\r\ntext") == "office\ntext"
    assert usable_text("Hello") and not usable_text("\ufffd\ufffd\ufffd")
    assert r"a\|b" in table_markdown([["a|b", "line\nline"], [None, "2"]])


async def noop_dns(url):
    pass


def run_mock(handler, opts, *, urls=None):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            f = Fetcher(client, time.monotonic() + 30, dns_check=noop_dns)
            rows = [await extract_url(url, opts, f) for url in (urls or opts["urls"])]
            return rows, f
    return asyncio.run(run())


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_http_error_free_and_no_retry(status, opts):
    calls = []
    def handler(req):
        calls.append(str(req.url))
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(status, headers={"Retry-After": "60"})
    rows, f = run_mock(handler, opts)
    row = rows[0]
    assert row["error"] == "http_error" and row["http_status"] == status
    assert row["billable"] is False and row["ocr_pages"] == 0
    assert row["retry_after"] == "60" and len(calls) == 2


def test_non_pdf_and_robots_cache(opts):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        return httpx.Response(200, text="<html>No PDF</html>")
    rows, f = run_mock(handler, opts, urls=opts["urls"] * 2)
    assert len(rows) == 2 and all(r["error"] == "not_a_pdf" and r["http_status"] == 200 for r in rows)
    assert f.requests == 3


def test_robots_refusal_no_pdf_request(opts):
    def handler(req):
        assert req.url.path == "/robots.txt"
        return httpx.Response(200, text="User-agent: *\nDisallow: /file.pdf\n")
    rows, f = run_mock(handler, opts)
    assert rows[0]["error"] == "robots_disallowed" and f.requests == 1


def test_private_redirect_never_requested(opts):
    calls = []
    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(302, headers={"Location": "http://127.0.0.1/secret"})
    rows, f = run_mock(handler, opts)
    assert rows[0]["error"] == "invalid_url" and f.requests == 2


def test_digital_pdf_through_real_worker(tmp_path, opts):
    pdf = document(tmp_path / "doc.pdf").read_bytes()
    def handler(req):
        return httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(200, content=pdf)
    original = "https://example.com/file.pdf#page=1"
    rows, _ = run_mock(handler, opts, urls=[original])
    assert rows[0]["url"] == original and rows[0]["page_count"] == 1
    assert rows[0]["ocr_pages"] == 0 and rows[0]["billable"] is True


def test_corrupt_pdf_through_worker(opts):
    def handler(req):
        return httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(200, content=b"%PDF-1.7\nno xref")
    rows, _ = run_mock(handler, opts)
    assert rows[0]["error"] == "corrupt_pdf" and rows[0]["http_status"] == 200


def test_no_work_after_run_deadline(opts):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("HTTP after deadline"))) as c:
            return await extract_url(opts["urls"][0], opts, Fetcher(c, time.monotonic() - 1, noop_dns))
    assert asyncio.run(run())["error"] == "time_limit"


class FakeActor:
    def __init__(self, capped=False):
        self.events, self.rows, self.capped = [], [], capped
    async def push_data(self, row, charged_event_name=None):
        self.rows.append(row)
        if charged_event_name:
            self.events.append((charged_event_name, 1))
        return SimpleNamespace(event_charge_limit_reached=self.capped if charged_event_name else False)
    async def charge(self, event_name, count):
        self.events.append((event_name, count))
        return SimpleNamespace(event_charge_limit_reached=False)


def test_error_row_never_billed():
    actor = FakeActor()
    row = diagnostic("https://example.com/file.pdf", FetchError("http_error", "HTTP 403", http_status=403))
    assert asyncio.run(persist(row, actor)) is False
    assert actor.rows == [row] and actor.events == []


@pytest.mark.parametrize("ocr_pages", [0, 1, 3])
def test_only_success_pdf_and_actual_ocr_events(ocr_pages):
    actor = FakeActor()
    assert asyncio.run(persist({"billable": True, "ocr_pages": ocr_pages}, actor)) is False
    assert actor.events == [("pdf-extracted", 1)] + ([("ocr-page", ocr_pages)] if ocr_pages else [])


def test_spending_limit_stops_secondary_charge():
    actor = FakeActor(capped=True)
    assert asyncio.run(persist({"billable": True, "ocr_pages": 2}, actor)) is True
    assert actor.events == [("pdf-extracted", 1)]


def test_robots_http_refusal_is_final(opts):
    def handler(req):
        assert req.url.path == "/robots.txt"
        return httpx.Response(403)
    rows, f = run_mock(handler, opts)
    assert rows[0]["http_status"] == 403 and rows[0]["billable"] is False
    assert f.requests == 1


def test_product_robots_cannot_override_wildcard_refusal(opts):
    def handler(req):
        assert req.url.path == "/robots.txt"
        return httpx.Response(200, text="User-agent: hiver-pdf-text-extractor\nAllow: /\nUser-agent: *\nDisallow: /file.pdf\n")
    rows, f = run_mock(handler, opts)
    assert rows[0]["error"] == "robots_disallowed" and f.requests == 1


def test_content_length_limit(opts):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=b"%PDF-1.7", headers={"Content-Length": str(26 * 1024 * 1024)})
    rows, _ = run_mock(handler, opts)
    assert rows[0]["error"] == "size_limit" and not rows[0]["billable"]


def test_partial_response_is_not_success(opts):
    def handler(req):
        return httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(206, content=b"%PDF-1.7")
    rows, _ = run_mock(handler, opts)
    assert rows[0]["error"] == "http_error" and rows[0]["http_status"] == 206


def test_header_over_scan_uses_ocr(tmp_path, opts):
    path = document(tmp_path / "header.pdf", scan=True)
    with pymupdf.open(path) as doc:
        doc[0].insert_text((70, 70), "1")
        updated = tmp_path / "header-scan.pdf"
        doc.save(updated)
    row = extract_pdf(updated, opts)
    assert row["ocr_pages"] == 1 and "POWERLIFTING" in row["text"]


def test_blank_page_in_digital_document_retained_without_ocr(tmp_path, opts):
    path = document(tmp_path / "digital.pdf")
    with pymupdf.open(path) as doc:
        doc.new_page()
        updated = tmp_path / "with-blank.pdf"
        doc.save(updated)
    row = extract_pdf(updated, opts)
    assert row["page_count"] == 2 and row["ocr_pages"] == 0
    assert row["pages"][1] == {"page": 2, "text": "", "ocr": False}


def test_ocr_failure_is_not_partial_success(tmp_path, opts, monkeypatch):
    path = document(tmp_path / "scan.pdf", scan=True, pages=2)
    def failed(*args):
        raise PDFError("ocr_failed", "OCR failed")
    monkeypatch.setattr("src.extractor.ocr_text", failed)
    with pytest.raises(PDFError) as error:
        extract_pdf(path, opts)
    assert error.value.code == "ocr_failed"


def test_dns_rejects_private_answers(monkeypatch):
    async def run():
        async def answers(*args, **kwargs):
            return [(2, 1, 6, "", ("10.0.0.1", 443))]
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", answers)
        with pytest.raises(FetchError) as error:
            await public_dns("https://example.com/file.pdf")
        assert error.value.code == "private_address"
    asyncio.run(run())
