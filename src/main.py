"""Apify adapter: charge only fully extracted PDFs; every error row is free.
SPDX-License-Identifier: AGPL-3.0-or-later
"""
import time

import httpx
from apify import Actor
from .network import HEADERS, FetchError, Fetcher, diagnostic, extract_url, options


async def persist(row, actor):
    if not row.get("billable") or row.get("error"):
        await actor.push_data(row)
        return False
    pushed = await actor.push_data(row, charged_event_name="pdf-extracted")
    if pushed.event_charge_limit_reached:
        return True
    if row["ocr_pages"]:
        charge = await actor.charge(event_name="ocr-page", count=row["ocr_pages"])
        return charge.event_charge_limit_reached
    return False


async def main():
    async with Actor:
        raw = await Actor.get_input() or {}
        try:
            opts = options(raw)
        except FetchError as error:
            await Actor.push_data(diagnostic(None, error))
            await Actor.set_status_message(error.reason)
            return
        started = time.monotonic()
        summary = {"pdfs_extracted": 0, "diagnostics": 0, "ocr_pages": 0, "custom_start_events": 0,
                   "user_agent": HEADERS["User-Agent"], "spending_limit_reached": False}
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=False, trust_env=False,
                                    limits=httpx.Limits(max_connections=1, max_keepalive_connections=1)) as client:
            fetcher = Fetcher(client, started + opts["runBudgetSeconds"])
            for url in opts["urls"]:
                if summary["spending_limit_reached"]:
                    # Do not download/extract additional PDFs after a platform charge cap.
                    row = diagnostic(url, FetchError("charge_limit", "Run charge cap reached before this PDF; no charge."))
                else:
                    row = await extract_url(url, opts, fetcher)
                limited = await persist(row, Actor)
                summary["spending_limit_reached"] |= limited
                if row.get("error"):
                    summary["diagnostics"] += 1
                elif not limited:
                    summary["pdfs_extracted"] += 1
                    summary["ocr_pages"] += row["ocr_pages"]
            summary["http_requests"] = fetcher.requests
        summary["seconds"] = round(time.monotonic() - started, 3)
        await Actor.set_value("SUMMARY", summary)
        await Actor.set_status_message(f"{summary['pdfs_extracted']} PDFs extracted; {summary['ocr_pages']} scanned pages OCRed; "
                                       f"{summary['diagnostics']} uncharged diagnostics. See SUMMARY for caps.")
