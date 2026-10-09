# PDF Text Extractor

Turn public PDF URLs into full text, page-delimited Markdown, per-page text, detected tables and source metadata. Use it for company reports, public filings, manuals, research papers and scanned business documents.

The Actor reads the PDF's text layer first. It runs Tesseract only on pages without a usable text layer, including scanned pages with only a page number or sparse header. Digital pages do not incur an OCR event. English and German are the default OCR languages.

**Retired experiment, not available on the Apify Store.** On 2026-10-09 this Actor failed hiver's quality bench twice: a 51-page PDF ran past its 60-second limit in Apify's cloud, so it was never published. The source stays here for reference and is not maintained. Links below to apify.com/hiver/pdf-text-extractor do not resolve, and the pricing section describes a price that never took effect.

## Input

```json
{
  "urls": ["https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"],
  "ocrLanguages": "eng+deu",
  "maxPages": 200,
  "pdfTimeoutSeconds": 60,
  "runBudgetSeconds": 240
}
```

Provide 1–100 public HTTP(S) URLs. The default Store run has 1,024 MB and a 300-second timeout. PDF download/extraction is sequential and bounded by the run budget. Entries not processed before that budget receive an uncharged diagnostic, not a missing or truncated result.

Installed OCR packs: `eng`, `deu`, `fra`, `spa`, `ita`, `por`, `nld`. Combine packs with `+`. This Actor does not accept passwords, cookies, login sessions or proxy settings.

## Output

One dataset row per submitted URL. Successful rows have:

- `url`: exact submitted URL; `final_url`: source after permitted redirects.
- `status`: `extracted`; `http_status`: final HTTP response code.
- `page_count`: the document's actual total number of pages.
- `text`: complete document text, with blank lines between pages.
- `markdown`: text blocks with page markers, detected heading emphasis and Markdown tables.
- `pages`: objects containing `page` (one-based), `text` and `ocr` (boolean).
- `tables`: detected digital ruling-line tables as `{ "page": 1, "markdown": "..." }`.
- `metadata`: the source's `title` and `author`, or null when absent. Values are not inferred.
- `char_count`: length of `text`; `ocr_pages`: count of pages actually OCRed.
- `billable`: true only on fully extracted PDFs. Optional `warnings` disclose unavailable table detection.

Example diagnostic shape (illustrative, not a measured live result):

```json
{
  "url": "https://example.com/private.pdf",
  "status": "error",
  "http_status": 403,
  "error": "http_error",
  "reason": "HTTP 403; no retry, proxy or alternative identity is attempted.",
  "billable": false,
  "page_count": null,
  "text": "",
  "markdown": "",
  "pages": [],
  "tables": [],
  "metadata": { "title": null, "author": null },
  "char_count": 0,
  "ocr_pages": 0
}
```

`SUMMARY` in the default key-value store records successful PDFs, OCR pages, uncharged diagnostics, work duration and spending-limit state.

## Pricing

- $0.002 per completely extracted PDF ($2 per 1,000 digital PDFs).
- $0.004 per scanned page OCRed in a completely extracted PDF ($4 per 1,000 OCR pages, in addition to the PDF event).
- No custom per-run start fee. HTTP errors, non-PDF responses, encrypted/corrupt documents, empty documents and timed-out or partial extractions incur neither extraction nor OCR events.
- The board's current Apify service retains Apify's native $0.00005 start event per run. This is a platform fee, not a custom event, and can also apply to error-only runs. Zeroing it has been requested; until confirmed, this Actor does not claim literally zero total start cost.

At the configured prices, one digital PDF costs $0.002 plus the native platform start event; one one-page scan costs $0.006 plus that event. Prices are configuration, not a certified comparison against competitors.

## Limits and source access

Only directly accessible public PDFs may be used. The Actor identifies itself as `hiver-pdf-text-extractor/0.1.0 (+https://apify.com/hiver/pdf-text-extractor)`, checks product and wildcard robots rules, and honors crawl delays. A 401, 403 or 429 stops that request immediately. It does not rotate identities, IPs, headers or proxies to get around a refusal.

Safety limits: 25 MiB downloads, 200 pages by default (maximum 500), up to 120 seconds per PDF, and 8 MiB serialized output per PDF. Large or slow documents return explicit uncharged errors; text is never silently clipped. Each PDF is parsed in a separate worker process. OCR rendering is capped at 20 million pixels per page, with a 35-second per-page OCR limit.

Text order depends on the source PDF's content streams. Complex columns, handwriting, very poor scans, unusual fonts, mathematics, diagrams and merged cells may not be reconstructed perfectly. Markdown is structural text, not an exact reproduction of the page. Table detection is limited to digital ruling-line tables; scanned/borderless tables are not promised. Source pages and URLs remain in the result so callers can verify extraction.

## FAQ

### Is OCR used on every page?

No. Pages with a usable digital text layer are read directly. OCR is reserved for pages that lack one, including scans under a sparse header. Completely blank pages are retained without OCR; an entirely textless document is an uncharged diagnostic.

### Does a bad URL fail the run?

An expected document or HTTP failure becomes an error row, while other URLs continue. Error-only input can finish SUCCEEDED. Infrastructure or platform failures are separate and cannot be promised away.

### Are failed OCR pages charged?

No. The Actor completes the entire PDF first. Any download, parse, OCR, time or safety-limit failure results in a diagnostic and no PDF/OCR event, even if some pages were processed internally.

### Can I extract encrypted PDFs?

No. Supply an unencrypted public document. The Actor does not try passwords or bypass copy restrictions.

## License

This Actor's source is licensed AGPL-3.0-or-later. It uses PyMuPDF (AGPL), Tesseract (Apache-2.0), httpx (BSD-3-Clause), Protego (BSD-3-Clause) and the Apify SDK (Apache-2.0). This repository holds the complete source. No external AI or paid OCR API is used.
