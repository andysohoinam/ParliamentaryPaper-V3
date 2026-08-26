#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import os
import re
import time
import urllib.robotparser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urljoin, urlparse, urlsplit

import pdfplumber
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

PARLIAMENT_BASE = "https://www.parliament.gov.sg"
ORDER_PAGE = PARLIAMENT_BASE + "/parliamentary-business/order-paper"
ORDER_DOCS = PARLIAMENT_BASE + "/docs/default-source/order-paper/"
SPRS_BASE = "https://sprs.parl.gov.sg"
SPRS_REPORT = SPRS_BASE + "/search/getHansardReport/?sittingDate={date}"

# Verified official URLs used only if the Parliament listing stays asynchronous.
ORDER_FALLBACKS = {
    "08-04-2026": (
        "Order Paper No. 28",
        PARLIAMENT_BASE + "/api/media/07fd3bdb-cb5f-64e2-b198-ff00006af031/order-paper---8apr2026.pdf",
    ),
    "04-03-2026": (
        "Order Paper No. 24",
        PARLIAMENT_BASE + "/api/media/02bb3bdb-cb5f-64e2-b198-ff00006af031/order-paper---4mar2026.pdf",
    ),
    "04-02-2026": (
        "Order Paper No. 16",
        PARLIAMENT_BASE + "/api/media/fe863bdb-cb5f-64e2-b198-ff00006af031/order-paper---4feb2026.pdf",
    ),
}

P_FIELDS = [
    "title", "sitting_date", "document_url", "pdf_url_used", "download_method",
    "http_status", "download_status", "download_bytes", "sha256", "page_count",
    "content_chars", "content_text", "local_pdf_path", "error",
]
S_FIELDS = [
    "parliament_no", "session_no", "volume_no", "sitting_no", "sitting_date",
    "section_type", "title", "sub_title", "question_no", "start_page", "end_page",
    "content_html", "content_text", "report_url",
]
L_FIELDS = ["timestamp_utc", "source", "stage", "url", "status", "reason", "rows"]
PDF_RE = re.compile(r'(?:https?://[^"\'<>\s]+|/[^"\'<>\s]+)\.pdf(?:\?[^"\'<>\s]*)?', re.I)


class AccessBlocked(RuntimeError):
    pass


@dataclass
class Event:
    timestamp_utc: str
    source: str
    stage: str
    url: str
    status: str
    reason: str
    rows: int | str = ""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def validate_date(value: str) -> str:
    try:
        datetime.strptime(value, "%d-%m-%Y")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use a valid DD-MM-YYYY date") from exc
    return value


def write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


class Client:
    def __init__(self, user_agent: str, interval: float, timeout: float, retries: int):
        self.user_agent = user_agent
        self.interval = interval
        self.timeout = timeout
        self.last_request: dict[str, float] = {}
        self.robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self.session = requests.Session()
        retry = Retry(
            total=retries,
            connect=retries,
            read=retries,
            status=retries,
            allowed_methods=frozenset({"GET", "HEAD"}),
            status_forcelist=(429, 500, 502, 503, 504),
            backoff_factor=1,
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept-Language": "en-SG,en;q=0.9",
        })

    def wait(self, host: str) -> None:
        delay = self.interval - (time.monotonic() - self.last_request.get(host, 0.0))
        if delay > 0:
            time.sleep(delay)
        self.last_request[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            robots_url = urljoin(origin, "/robots.txt")
            self.wait(parts.netloc)
            response = self.session.get(robots_url, timeout=self.timeout, allow_redirects=True)
            if response.status_code in (401, 403):
                raise AccessBlocked(f"robots.txt HTTP {response.status_code}")
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(robots_url)
            if 400 <= response.status_code < 500:
                # RFC 9309: unavailable robots.txt in this range means no rules supplied.
                parser.parse(["User-agent: *", "Allow: /"])
            elif response.status_code >= 500:
                raise RuntimeError(f"robots.txt temporarily unavailable: HTTP {response.status_code}")
            else:
                response.raise_for_status()
                parser.parse(response.text.splitlines())
            self.robots[origin] = parser
        return self.robots[origin].can_fetch(self.user_agent, url)

    def get(self, url: str, ready_attempts: int = 1, **kwargs: Any) -> requests.Response:
        if not self.allowed(url):
            raise AccessBlocked(f"robots.txt disallows {url}")
        last: requests.Response | None = None
        for attempt in range(ready_attempts):
            self.wait(urlsplit(url).netloc)
            last = self.session.get(url, timeout=self.timeout, allow_redirects=True, **kwargs)
            if last.status_code in (401, 403):
                raise AccessBlocked(f"HTTP {last.status_code}")
            if last.status_code == 202 and attempt + 1 < ready_attempts:
                try:
                    delay = float(last.headers.get("Retry-After", ""))
                except ValueError:
                    delay = min(2 ** (attempt + 1), 10)
                time.sleep(delay)
                continue
            if last.status_code != 200:
                raise requests.HTTPError(
                    f"expected HTTP 200, received {last.status_code}", response=last
                )
            return last
        raise requests.HTTPError("resource remained unavailable", response=last)

    def close(self) -> None:
        self.session.close()


def pdf_links(body: bytes, base_url: str) -> list[tuple[str, str]]:
    text = body.decode("utf-8", errors="ignore")
    soup = BeautifulSoup(text, "html.parser")
    found: dict[str, str] = {}
    for anchor in soup.select("a[href]"):
        url = urljoin(base_url, anchor.get("href", ""))
        if urlparse(url).path.lower().endswith(".pdf"):
            found[url] = clean(anchor.get_text(" ")) or Path(unquote(urlparse(url).path)).name
    for value in PDF_RE.findall(text):
        url = urljoin(base_url, html.unescape(value))
        found.setdefault(url, Path(unquote(urlparse(url).path)).name)
    return [(title, url) for url, title in found.items()]


def candidate_pdf_urls(url: str) -> list[str]:
    urls = [url]
    name = Path(unquote(urlparse(url).path)).name
    if name.lower().endswith(".pdf"):
        alternate = ORDER_DOCS + name
        if alternate not in urls:
            urls.append(alternate)
    return urls


def browser_pdf(urls: list[str], timeout: float, user_agent: str) -> tuple[bytes, str, str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return b"", urls[0], f"playwright_not_installed:{exc}"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(user_agent=user_agent, locale="en-SG")
            page = context.new_page()
            for url in urls:
                try:
                    response = page.goto(url, wait_until="commit", timeout=int(timeout * 1000))
                    if response is not None:
                        body = response.body()
                        if body.lstrip().startswith(b"%PDF"):
                            browser.close()
                            return body, response.url, "playwright-navigation"
                except Exception:
                    pass
                try:
                    response = context.request.get(
                        url,
                        headers={"Accept": "application/pdf,*/*", "Referer": ORDER_PAGE},
                        timeout=int(timeout * 1000),
                        fail_on_status_code=False,
                    )
                    body = response.body()
                    if body.lstrip().startswith(b"%PDF"):
                        browser.close()
                        return body, response.url, "playwright-context-request"
                except Exception:
                    pass
            browser.close()
    except Exception as exc:
        return b"", urls[0], f"playwright_error:{type(exc).__name__}:{clean(exc)}"
    return b"", urls[-1], "browser_non_pdf_response"


def extract_pdf(data: bytes) -> tuple[int, str]:
    if not data.lstrip().startswith(b"%PDF"):
        raise ValueError("response is not PDF")
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = [(page.extract_text() or "") for page in pdf.pages]
    return len(pages), clean("\n".join(pages))


def find_sections(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("takesSectionVOList", "takesSectionVoList", "sectionList"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        for value in payload.values():
            found = find_sections(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_sections(value)
            if found:
                return found
    return []


def strip_html(value: str) -> str:
    soup = BeautifulSoup(html.unescape(value or ""), "html.parser")
    return clean(soup.get_text("\n", strip=True))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=("all", "parliament", "sprs"), default="all")
    parser.add_argument("--sprs-dates", nargs="*", type=validate_date, default=[])
    parser.add_argument("--parliament-limit", type=int, default=20)
    parser.add_argument("--min-interval", type=float, default=3)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--output-dir", type=Path, default=Path("crawler_output"))
    parser.add_argument(
        "--user-agent",
        default=os.getenv(
            "CRAWLER_USER_AGENT",
            "ParliamentResearchCrawler/1.0 (contact: repository-issues)",
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    parliament_rows: list[dict[str, Any]] = []
    sprs_rows: list[dict[str, Any]] = []
    events: list[Event] = []
    successes = 0
    client = Client(args.user_agent, args.min_interval, args.timeout, args.max_retries)
    try:
        if args.source in ("all", "parliament"):
            links: list[tuple[str, str, str]] = []
            try:
                response = client.get(ORDER_PAGE, ready_attempts=4)
                links = [(title, url, "") for title, url in pdf_links(response.content, response.url)]
                if not links:
                    raise RuntimeError("listing returned no PDF links")
                events.append(Event(now(), "parliament", "listing", response.url, "200", "ok", len(links)))
            except Exception as exc:
                events.append(Event(now(), "parliament", "listing", ORDER_PAGE, "0", f"{type(exc).__name__}: {clean(exc)}", 0))

            if not links:
                selected_dates = set(args.sprs_dates)
                for date, (title, url) in ORDER_FALLBACKS.items():
                    if not selected_dates or date in selected_dates:
                        links.append((title, url, date))
                events.append(Event(now(), "parliament", "fallback", ORDER_PAGE, "0", "used verified official URLs", len(links)))

            for title, url, sitting_date in links[: args.parliament_limit or None]:
                row = {field: "" for field in P_FIELDS}
                row.update(title=title, sitting_date=sitting_date, document_url=url)
                data = b""
                final_url = ""
                method = ""
                last_error = ""
                for candidate in candidate_pdf_urls(url):
                    try:
                        response = client.get(candidate, headers={"Accept": "application/pdf,*/*"})
                        if response.content.lstrip().startswith(b"%PDF"):
                            data, final_url, method = response.content, response.url, "requests"
                            break
                        last_error = "non_pdf_response"
                    except Exception as exc:
                        last_error = f"{type(exc).__name__}: {clean(exc)}"
                if not data:
                    data, final_url, method = browser_pdf(candidate_pdf_urls(url), args.timeout, args.user_agent)
                    if not data:
                        last_error = method
                if data:
                    try:
                        pages, text = extract_pdf(data)
                        name = hashlib.sha256(url.encode()).hexdigest()[:16] + ".pdf"
                        target = out / "pdf" / name
                        target.parent.mkdir(exist_ok=True)
                        target.write_bytes(data)
                        row.update(
                            pdf_url_used=final_url,
                            download_method=method,
                            http_status=200,
                            download_status="success",
                            download_bytes=len(data),
                            sha256=hashlib.sha256(data).hexdigest(),
                            page_count=pages,
                            content_chars=len(text),
                            content_text=text,
                            local_pdf_path=f"pdf/{name}",
                        )
                        successes += 1
                    except Exception as exc:
                        row.update(download_status="failed", error=f"{type(exc).__name__}: {clean(exc)}")
                else:
                    row.update(download_status="failed", error=last_error or "download_failed")
                parliament_rows.append(row)

        if args.source in ("all", "sprs"):
            for date in args.sprs_dates:
                url = SPRS_REPORT.format(date=date)
                try:
                    response = client.get(url, headers={"Accept": "application/json"})
                    payload = response.json()
                    meta = payload.get("metadata") or {}
                    sections = find_sections(payload)
                    added = 0
                    for section in sections:
                        content_html = section.get("content", "") or ""
                        content_text = strip_html(content_html)
                        if not content_text:
                            continue
                        sprs_rows.append({
                            "parliament_no": meta.get("parlimentNO", ""),
                            "session_no": meta.get("sessionNO", ""),
                            "volume_no": meta.get("volumeNO", ""),
                            "sitting_no": meta.get("sittingNO", ""),
                            "sitting_date": meta.get("sittingDate", date),
                            "section_type": section.get("sectionType", ""),
                            "title": section.get("title", ""),
                            "sub_title": section.get("subTitle", "") or "",
                            "question_no": section.get("questionNo", "") or "",
                            "start_page": section.get("startPgNo", ""),
                            "end_page": section.get("endPgNo", ""),
                            "content_html": content_html,
                            "content_text": content_text,
                            "report_url": response.url,
                        })
                        added += 1
                    if not added:
                        raise ValueError("SPRS response contained no text sections")
                    events.append(Event(now(), "sprs", "report", response.url, "200", "ok", added))
                    successes += 1
                except Exception as exc:
                    events.append(Event(now(), "sprs", "report", url, "0", f"{type(exc).__name__}: {clean(exc)}", 0))
    finally:
        client.close()

    write_csv(out / "parliament_order_papers.csv", P_FIELDS, parliament_rows)
    write_csv(out / "sprs_official_report_sections.csv", S_FIELDS, sprs_rows)
    write_csv(out / "crawl_status.csv", L_FIELDS, (asdict(event) for event in events))
    summary = {
        "parliament_rows": len(parliament_rows),
        "parliament_success": sum(row.get("download_status") == "success" for row in parliament_rows),
        "sprs_rows": len(sprs_rows),
        "successful_fetches": successes,
        "status_rows": len(events),
    }
    (out / "crawl_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if successes else 1


if __name__ == "__main__":
    raise SystemExit(main())
