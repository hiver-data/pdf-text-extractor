"""Low-volume developer live smoke; never a substitute for the independent bench."""
import asyncio
import json
import os
import time
from pathlib import Path
import httpx
from src.network import HEADERS, Fetcher, extract_url, options

URLS = [
    "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf",
    "https://arxiv.org/pdf/1706.03762",
    "https://www.who.int/docs/default-source/coronaviruse/situation-reports/20200121-sitrep-1-2019-ncov.pdf",
    "https://www.gesetze-im-internet.de/gg/GG.pdf",
    "https://upload.wikimedia.org/wikipedia/commons/6/6e/Scan_ADFPA_Letter.pdf",
    "https://www.irs.gov/pub/irs-pdf/fw9.pdf",
    "https://www.cbo.gov/system/files/2024-02/59710-Outlook-2024.pdf",
]

async def run():
    output = Path(os.environ.get("PDF_DEVELOPER_OUTPUT_DIR", "verification"))
    output.mkdir(exist_ok=True)
    started = time.monotonic()
    opts = options({"urls": URLS})
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=False, trust_env=False) as client:
        fetcher = Fetcher(client, started + 240)
        rows = []
        for i, url in enumerate(URLS):
            row = await extract_url(url, opts, fetcher)
            rows.append(row)
            print(json.dumps({k: row.get(k) for k in ["url", "status", "http_status", "page_count", "ocr_pages", "char_count", "processing_seconds", "error", "reason"]}, ensure_ascii=False), flush=True)
            (output / f"live-{i}.json").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    summary = {"kind": "developer_live_smoke_not_bench", "seconds": round(time.monotonic()-started, 3),
               "rows": [{k:r.get(k) for k in ["url","status","http_status","page_count","ocr_pages","char_count","error","reason"]} for r in rows]}
    (output / "live-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary), flush=True)

if __name__ == "__main__":
    asyncio.run(run())
