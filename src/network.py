"""Bounded direct downloads; truthful UA, robots checks, no retries on refusals.
SPDX-License-Identifier: AGPL-3.0-or-later
"""
import asyncio
import ipaddress
import json
import os
import re
import signal
import socket
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from protego import Protego

UA = "hiver-pdf-text-extractor/0.1.0 (+https://apify.com/hiver/pdf-text-extractor)"
HEADERS = {"User-Agent": UA, "Accept": "application/pdf, text/plain;q=0.5, */*;q=0.1", "Accept-Language": "en"}
MAX_PDF_BYTES = 25 * 1024 * 1024


class FetchError(Exception):
    def __init__(self, code, reason, **context):
        super().__init__(reason)
        self.code, self.reason, self.context = code, reason, context


def options(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("urls"), list) or not 1 <= len(raw["urls"]) <= 100:
        raise FetchError("invalid_input", "Provide urls as an array of 1–100 public HTTP(S) PDF URLs.")
    opts = {"urls": raw["urls"]}
    for name, default, low, high in [("maxPages", 200, 1, 500), ("runBudgetSeconds", 240, 10, 240),
                                     ("pdfTimeoutSeconds", 60, 10, 120)]:
        value = raw.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise FetchError("invalid_input", f"{name} must be an integer from {low} to {high}.")
        opts[name] = value
    languages = raw.get("ocrLanguages", "eng+deu")
    if not isinstance(languages, str) or not re.fullmatch(r"[a-z]{3}(?:\+[a-z]{3})*", languages):
        raise FetchError("invalid_input", "ocrLanguages must use Tesseract codes, for example eng+deu.")
    supported = {"eng", "deu", "fra", "spa", "ita", "por", "nld"}
    if not set(languages.split("+")) <= supported:
        raise FetchError("invalid_input", "Unsupported OCR language. Installed packs: eng, deu, fra, spa, ita, por, nld.")
    opts["ocrLanguages"] = languages
    return opts


def public_url(raw):
    # Keep the submitted identifier unchanged in output; normalization is request-only.
    if not isinstance(raw, str) or not raw or any(ord(c) < 33 for c in raw) or "\\" in raw:
        raise FetchError("invalid_url", "Expected an absolute public HTTP(S) URL without whitespace or credentials.")
    try:
        p = urlsplit(raw)
        if p.scheme not in ("https", "http") or not p.hostname or p.username is not None or p.password is not None or p.port not in (None, 80, 443):
            raise ValueError()
        host = p.hostname.encode("idna").decode("ascii").lower()
        if host == "localhost" or host.endswith((".local", ".localhost", ".internal")) or "." not in host:
            raise ValueError()
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            if re.fullmatch(r"[0-9.]+", host) or ":" in host:
                raise ValueError()
        else:
            if not ip.is_global:
                raise ValueError()
        return urlunsplit((p.scheme, p.netloc, p.path or "/", p.query, ""))
    except (ValueError, UnicodeError):
        raise FetchError("invalid_url", "Only public HTTP(S) URLs on ports 80/443 without credentials are allowed.") from None


async def public_dns(url):
    p = urlsplit(url)
    try:
        records = await asyncio.get_running_loop().getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError:
        raise FetchError("dns_error", "The public host could not be resolved.") from None
    if not records or any(not ipaddress.ip_address(r[4][0]).is_global for r in records):
        raise FetchError("private_address", "Requests to non-public network addresses are not permitted.")


def origin(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "", "", ""))


def diagnostic(url, error, **context):
    return {"url": url, "status": "error", "error": error.code, "reason": error.reason,
            "http_status": None, "page_count": None, "text": "", "markdown": "", "pages": [], "tables": [],
            "metadata": {"title": None, "author": None}, "char_count": 0, "ocr_pages": 0,
            "billable": False, **context, **error.context}


class Fetcher:
    def __init__(self, client, deadline, dns_check=public_dns):
        self.client, self.deadline, self.dns_check = client, deadline, dns_check
        self.robots, self.last_request, self.delays = {}, {}, {}
        self.requests = 0

    async def request(self, url, max_bytes):
        url = public_url(url)
        await self.dns_check(url)
        host = origin(url)
        delay = self.delays.get(host, 0)
        wait = max(0, self.last_request.get(host, 0) + delay - time.monotonic())
        if time.monotonic() + wait >= self.deadline:
            raise FetchError("time_limit", "Time budget exhausted before the next permitted request.")
        if wait:
            await asyncio.sleep(wait)  # site politeness, never a board-service wait
        self.last_request[host] = time.monotonic()
        self.requests += 1
        try:
            async with self.client.stream("GET", url, timeout=min(20, max(0.1, self.deadline - time.monotonic()))) as response:
                status = response.status_code
                if status >= 400:
                    raise FetchError("http_error", f"HTTP {status}; no retry, proxy or alternative identity is attempted.",
                                     http_status=status, final_url=str(response.url), retry_after=response.headers.get("retry-after"))
                if status in (301, 302, 303, 307, 308):
                    return status, response.headers, b""
                if status != 200:
                    raise FetchError("http_error", f"Expected a complete HTTP 200 response, received {status}.", http_status=status)
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > max_bytes:
                    raise FetchError("size_limit", "Response exceeds the download safety limit.", http_status=status)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise FetchError("size_limit", "Response exceeds the download safety limit.", http_status=status)
                    if time.monotonic() >= self.deadline:
                        raise FetchError("time_limit", "Time budget exhausted during the download.", http_status=status)
                return status, response.headers, bytes(body)
        except httpx.TimeoutException:
            raise FetchError("download_timeout", "PDF/robots download timed out; no retry is attempted.") from None
        except httpx.HTTPError:
            raise FetchError("network_error", "The direct public HTTP request failed; no retry is attempted.") from None

    async def allowed(self, url):
        host = origin(url)
        if host not in self.robots:
            current = host + "/robots.txt"
            try:
                for _ in range(6):
                    status, headers, body = await self.request(current, 512 * 1024)
                    if status == 200:
                        policy = Protego.parse(body.decode("utf-8", errors="replace"))
                        self.robots[host] = policy
                        self.delays[host] = max(policy.crawl_delay(UA) or 0, policy.crawl_delay("*") or 0)
                        break
                    target = public_url(urljoin(current, headers.get("location", "")))
                    if target == current:
                        raise FetchError("redirect_error", "robots.txt redirected without a usable location.")
                    current = target
                else:
                    raise FetchError("redirect_limit", "robots.txt exceeded the redirect limit.")
            except FetchError as error:
                if error.context.get("http_status") in (404, 410):
                    self.robots[host] = None
                else:
                    raise
        policy = self.robots[host]
        if policy is not None and (not policy.can_fetch(url, UA) or not policy.can_fetch(url, "*")):
            raise FetchError("robots_disallowed", "robots.txt does not permit this PDF URL for this product or wildcard agent.")

    async def download(self, url):
        current = public_url(url)
        for _ in range(6):
            await self.allowed(current)
            status, headers, body = await self.request(current, MAX_PDF_BYTES)
            if status == 200:
                if b"%PDF-" not in body[:1024]:
                    raise FetchError("not_a_pdf", "The HTTP response is not a PDF document.", http_status=status, final_url=current)
                return body, current, status
            target = public_url(urljoin(current, headers.get("location", "")))
            if target == current:
                raise FetchError("redirect_error", "The PDF response redirected without a usable location.", http_status=status)
            current = target
        raise FetchError("redirect_limit", "The PDF exceeded the redirect limit.")


async def parse_in_worker(body, opts, seconds):
    with tempfile.TemporaryDirectory(prefix="pdf-worker-") as temp:
        path, output = Path(temp) / "input.pdf", Path(temp) / "result.json"
        path.write_bytes(body)
        settings = {"maxPages": opts["maxPages"], "ocrLanguages": opts["ocrLanguages"], "processingSeconds": seconds}
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "src.extractor", str(path), json.dumps(settings), str(output),
                                                      stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                                                      start_new_session=True)
        try:
            await asyncio.wait_for(process.wait(), timeout=max(0.1, seconds))
        except (TimeoutError, asyncio.CancelledError):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            raise
        if process.returncode or not output.exists():
            raise FetchError("extraction_failed", "PDF parser failed or exhausted memory; no extraction event is charged.")
        row = json.loads(output.read_text(encoding="utf-8"))
        if row.get("error"):
            raise FetchError(row["error"], row["reason"])
        return row


async def extract_url(url, opts, fetcher):
    final_url, status = None, None
    seconds = min(opts["pdfTimeoutSeconds"], fetcher.deadline - time.monotonic())
    if seconds <= 0:
        return diagnostic(url, FetchError("time_limit", "Run time budget exhausted before this PDF; no charge."))
    started = time.monotonic()
    try:
        async with asyncio.timeout(seconds):
            body, final_url, status = await fetcher.download(url)
            row = await parse_in_worker(body, opts, seconds - (time.monotonic() - started))
        return {"url": url, "http_status": status, "final_url": final_url, **row}
    except FetchError as error:
        return diagnostic(url, error, http_status=status, final_url=final_url)
    except TimeoutError:
        return diagnostic(url, FetchError("time_limit", "PDF time budget exhausted; no partial PDF or OCR event is charged."),
                          http_status=status, final_url=final_url)
