"""Exercise the real SDK adapter against generated PDF + isolated HTTP fixtures."""
import asyncio
import json
import os
from pathlib import Path

os.environ["APIFY_LOCAL_STORAGE_DIR"] = str(Path(os.environ["TMPDIR"]) / "pdf-sdk-storage")

import httpx
import pymupdf
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
from src import main as adapter
from apify import Actor
from src.network import Fetcher

# The real CLI exits the process; disable that only so this test can verify storage.
adapter.Actor = Actor(exit_process=False)

original_client = httpx.AsyncClient


def pdf_bytes(scan=False):
    with pymupdf.open() as doc:
        page = doc.new_page()
        if scan:
            image = Image.new("RGB", (1400, 1900), "white")
            draw = ImageDraw.Draw(image)
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 48)
            draw.text((140, 200), "Scanned company document", font=font, fill="black")
            draw.text((140, 280), "Revenue report for testing", font=font, fill="black")
            buf = BytesIO()
            image.save(buf, format="PNG")
            page.insert_image(page.rect, stream=buf.getvalue())
        else:
            page.insert_text((72, 90), "Company report: digital text layer. No OCR needed.")
        return doc.tobytes()


def response(request):
    if request.url.path == "/robots.txt":
        return httpx.Response(404)
    if request.url.path == "/digital.pdf":
        return httpx.Response(200, content=digital)
    if request.url.path == "/scan.pdf":
        return httpx.Response(200, content=scan)
    if request.url.path == "/denied.pdf":
        return httpx.Response(403)
    return httpx.Response(200, content=b"%PDF-1.7\ncorrupt")


async def no_dns(_):
    pass


def local_client(**kwargs):
    return original_client(**kwargs, transport=httpx.MockTransport(response))


def local_fetcher(client, deadline):
    return Fetcher(client, deadline, dns_check=no_dns)


digital, scan = pdf_bytes(), pdf_bytes(scan=True)
path = Path(os.environ["APIFY_LOCAL_STORAGE_DIR"])
input_path = path / "key_value_stores/default/INPUT.json"
input_path.parent.mkdir(parents=True, exist_ok=True)
input_path.write_text(json.dumps({"urls": ["https://example.com/" + name for name in ["digital.pdf", "scan.pdf", "denied.pdf", "corrupt.pdf"]]}))
adapter.httpx.AsyncClient = local_client
adapter.Fetcher = local_fetcher
asyncio.run(adapter.main())
rows = [json.loads(p.read_text()) for p in sorted((path / "datasets/default").glob("[0-9]*.json"))]
assert len(rows) == 4, rows
assert rows[0]["ocr_pages"] == 0 and rows[1]["ocr_pages"] == 1
assert "Scanned company document" in rows[1]["text"]
assert rows[2]["error"] == "http_error" and rows[2]["http_status"] == 403 and not rows[2]["billable"]
assert rows[3]["error"] == "corrupt_pdf" and not rows[3]["billable"]
summary = json.loads((path / "key_value_stores/default/SUMMARY").read_text())
assert summary["pdfs_extracted"] == 2 and summary["diagnostics"] == 2 and summary["ocr_pages"] == 1
assert summary["custom_start_events"] == 0
print("Real local SDK adapter PASS: 4 rows, 2 extracted PDFs, 1 OCR page, 2 uncharged diagnostics. Native cloud billing not measured.")
print(json.dumps(summary))
