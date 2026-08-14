"""Shared discovery for public Madgex job-board sitemaps.

The sitemap is the source of truth for discovery.  Successfully inspected IDs
are retained compactly in scan state, while only jobs that still need an LLM
decision are kept in the pending file.
"""

from __future__ import annotations

import html
import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

import requests


DEFAULT_SITEMAP_URL = "https://jobs.chronicle.com/sitemap2-1.xml"
DEFAULT_JOB_URL = "https://jobs.chronicle.com/job/{job_id}"
DEFAULT_WORKERS = 16
MAX_DESCRIPTION_CHARS = 16000
MAX_RETRY_DELAY = 60.0


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_date(value: Any) -> str:
    text = str(value or "").strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    return match.group(1) if match else ""


class _TextExtractor(HTMLParser):
    BLOCKS = {"br", "div", "h1", "h2", "h3", "li", "ol", "p", "table", "td", "th", "tr", "ul"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Any]) -> None:
        if tag.lower() in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: Any) -> str:
    parser = _TextExtractor()
    parser.feed(str(value or ""))
    parser.close()
    text = html.unescape("".join(parser.parts)).replace("\xa0", " ")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def parse_sitemap(xml_text: str) -> Dict[int, str]:
    """Return numeric job IDs and canonical URLs from a Madgex sitemap."""
    # Madgex serves this XML without an HTTP charset, so requests may decode
    # the UTF-8 BOM as the visible Latin-1 sequence ``ï»¿``.
    root = ET.fromstring(xml_text.lstrip("\ufeffï»¿ \t\r\n"))
    jobs: Dict[int, str] = {}
    for element in root.iter():
        if not element.tag.endswith("loc") or not element.text:
            continue
        url = element.text.strip()
        match = re.search(r"/job/(\d+)(?:/|$)", url)
        if match:
            jobs[int(match.group(1))] = url
    return jobs


def is_title_candidate(title: Any) -> bool:
    """Keep faculty-like titles after applying the shared exclusions."""
    lowered = str(title or "").lower()
    excluded = (
        "adjunct",
        "chair",
        "dean",
        "fellowship",
        "instructor",
        "lecturer",
        "post-doc",
        "postdoc",
        "temporary",
        "visiting",
    )
    if any(term in lowered for term in excluded):
        return False
    if re.search(r"\bnon[\s-]+tenure[\s-]+track\b", lowered):
        return False
    return bool(
        re.search(
            r"\bprofessor\b|\bfaculty\b|\bopen[\s-]+rank\b|\btenure[\s-]+track\b",
            lowered,
        )
    )


_DATA_LAYER_RE = re.compile(
    r"var\s+ClientGoogleTagManagerDataLayer\s*=\s*(\[.*?\])\s*</script>",
    re.IGNORECASE | re.DOTALL,
)
_DESCRIPTION_RE = re.compile(
    r"<section\b[^>]*\bid=[\"']job-description[\"'][^>]*>(.*?)"
    r"<section\b[^>]*\bid=[\"']company-information[\"']",
    re.IGNORECASE | re.DOTALL,
)
_LEGACY_DESCRIPTION_RE = re.compile(
    r"<h2\b[^>]*>\s*Job Details\s*</h2>(.*?)"
    r"<div\b[^>]*class=[\"'][^\"']*mds-surface__inner[^\"']*mds-border-top[^\"']*[\"']",
    re.IGNORECASE | re.DOTALL,
)
_EXPIRED_RE = re.compile(r">\s*This job has expired\s*<", re.IGNORECASE)


def parse_job_html(page_html: str, expected_id: int, observed_at: Optional[str] = None) -> Dict[str, Any]:
    """Extract only the fields needed for title filtering and LLM summarization."""
    match = _DATA_LAYER_RE.search(page_html)
    if not match:
        raise ValueError("Madgex job data layer was not found")
    payload = json.loads(match.group(1))
    data = next(
        (
            item
            for item in payload
            if isinstance(item, dict) and str(item.get("JobId") or "") == str(int(expected_id))
        ),
        None,
    )
    if data is None:
        raise ValueError(f"Madgex job data did not match expected ID {expected_id}")

    description_match = _DESCRIPTION_RE.search(page_html) or _LEGACY_DESCRIPTION_RE.search(page_html)
    description = html_to_text(description_match.group(1) if description_match else "")
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS].rstrip() + "…"
    # Keep the pending record limited to fields consumed by the summarizer.
    return {
        "id": int(expected_id),
        "title": str(data.get("job") or "").strip(),
        "institution": str(data.get("recruiter") or "").strip(),
        "location": str(data.get("LocationDescription") or "").strip(),
        "postedDate": normalize_date(data.get("JobDatePosted")),
        "description": description,
    }


@dataclass(frozen=True)
class FetchResult:
    kind: str
    status_code: Optional[int] = None
    job: Optional[Dict[str, Any]] = None
    error: str = ""


class MadgexClient:
    """Thread-safe client with bounded request starts and shared backoff."""

    def __init__(
        self,
        *,
        session: Optional[requests.Session] = None,
        request_delay: float = 0.02,
        timeout: float = 30,
        retry_count: int = 4,
        retry_delay: float = 2,
        sleep: Callable[[float], None] = time.sleep,
        sitemap_url: str = DEFAULT_SITEMAP_URL,
        source_name: str = "Chronicle",
    ) -> None:
        self.session = session
        self._session_local = threading.local()
        self.request_delay = max(0.0, float(request_delay))
        self.timeout = float(timeout)
        self.retry_count = max(1, int(retry_count))
        self.retry_delay = max(0.0, float(retry_delay))
        self.sleep = sleep
        self.sitemap_url = str(sitemap_url)
        self.source_name = str(source_name)
        self._request_lock = threading.Lock()
        self._last_request_at: Optional[float] = None
        self._cooldown_until = 0.0
        self.headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": "ArxivSummaryDaily/0.1 (personal academic job monitor)",
        }

    def _throttle(self) -> None:
        while True:
            with self._request_lock:
                now = time.monotonic()
                wait_for = max(0.0, self._cooldown_until - now)
                if self._last_request_at is not None:
                    wait_for = max(wait_for, self.request_delay - (now - self._last_request_at))
                if wait_for <= 0:
                    self._last_request_at = now
                    return
            # Do not hold the shared lock through a server-requested cooldown;
            # otherwise one sleeping worker serializes every other worker.
            self.sleep(wait_for)

    def _penalize(self, seconds: float) -> None:
        with self._request_lock:
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + max(0.0, seconds))

    def _get(self, url: str) -> requests.Response:
        self._throttle()
        session = self.session
        if session is None:
            session = getattr(self._session_local, "session", None)
            if session is None:
                session = requests.Session()
                self._session_local.session = session
        return session.get(url, headers=self.headers, timeout=self.timeout)

    def _retry_seconds(self, response: Optional[requests.Response], attempt: int) -> float:
        retry_after = "" if response is None else response.headers.get("Retry-After", "")
        try:
            return min(MAX_RETRY_DELAY, max(0.0, float(retry_after)))
        except (TypeError, ValueError):
            return min(MAX_RETRY_DELAY, self.retry_delay * (2**attempt))

    def _fetch_text(self, url: str) -> requests.Response:
        last_error = ""
        response: Optional[requests.Response] = None
        for attempt in range(self.retry_count):
            try:
                response = self._get(url)
                if response.status_code != 429 and response.status_code < 500:
                    return response
                last_error = f"HTTP {response.status_code}"
            except requests.RequestException as exc:
                last_error = str(exc)
                response = None
            if attempt + 1 < self.retry_count:
                delay = self._retry_seconds(response, attempt)
                self._penalize(delay)
                self.sleep(delay)
        raise RuntimeError(last_error or f"failed to fetch {url}")

    def discover_jobs(self) -> Dict[int, str]:
        response = self._fetch_text(self.sitemap_url)
        if response.status_code != 200:
            raise RuntimeError(f"{self.source_name} sitemap returned HTTP {response.status_code}")
        jobs = parse_sitemap(response.text)
        if not jobs:
            raise RuntimeError(f"{self.source_name} sitemap contained no job URLs")
        return jobs

    def fetch_job(self, job_id: int, url: str) -> FetchResult:
        try:
            response = self._fetch_text(url)
            if response.status_code == 404:
                return FetchResult("not_found", 404)
            if response.status_code in {401, 403}:
                return FetchResult("blocked", response.status_code)
            if response.status_code != 200:
                return FetchResult("error", response.status_code, error=f"HTTP {response.status_code}")
            if _EXPIRED_RE.search(response.text):
                return FetchResult("expired", 200)
            return FetchResult("job", 200, parse_job_html(response.text, job_id))
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return FetchResult("error", error=str(exc))


class MadgexStore:
    """Compact seen-ID state plus the queue of jobs awaiting summarization."""

    def __init__(self, pending_path: Path, state_path: Path) -> None:
        self.pending_path = Path(pending_path)
        self.state_path = Path(state_path)
        self.pending = self._load_json(
            self.pending_path,
            {"schemaVersion": 1, "generatedAt": "", "jobs": []},
        )
        self.state = self._load_json(
            self.state_path,
            {
                "schemaVersion": 1,
                "bootstrapComplete": False,
                "lastRunAt": "",
                "lastSitemapCount": 0,
                "lastSitemapMaxId": None,
                "seenIds": [],
                "pendingErrors": {},
            },
        )
        if not isinstance(self.pending.get("jobs"), list):
            self.pending["jobs"] = []
        if not isinstance(self.state.get("seenIds"), list):
            self.state["seenIds"] = []
        if not isinstance(self.state.get("pendingErrors"), dict):
            self.state["pendingErrors"] = {}
        self._seen_ids: Set[int] = {
            int(value) for value in self.state["seenIds"] if str(value).isdigit()
        }
        self._pending_by_id: Dict[int, Dict[str, Any]] = {
            int(job["id"]): job
            for job in self.pending["jobs"]
            if isinstance(job, dict) and str(job.get("id", "")).isdigit()
        }
        self._seen_ids.update(self._pending_by_id)

    @staticmethod
    def _load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
        if not path.exists():
            return dict(default)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            return payload if isinstance(payload, dict) else dict(default)
        except (OSError, json.JSONDecodeError):
            return dict(default)

    @staticmethod
    def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A process-specific temporary name preserves atomic replacement even
        # if an operator accidentally starts two resumable scans at once.
        temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        temporary.replace(path)

    @property
    def seen_ids(self) -> Set[int]:
        return set(self._seen_ids)

    @property
    def pending_jobs(self) -> List[Dict[str, Any]]:
        return list(self._pending_by_id.values())

    def add_job(self, job: Dict[str, Any]) -> bool:
        job_id = int(job["id"])
        if job_id in self._seen_ids:
            return False
        self._seen_ids.add(job_id)
        self._pending_by_id[job_id] = job
        self.state["pendingErrors"].pop(str(job_id), None)
        return True

    def mark_seen(self, job_id: int) -> None:
        job_id = int(job_id)
        self._seen_ids.add(job_id)
        self.state["pendingErrors"].pop(str(job_id), None)

    def record_error(self, job_id: int, error: str) -> None:
        key = str(int(job_id))
        previous = self.state["pendingErrors"].get(key, {})
        self.state["pendingErrors"][key] = {
            "attempts": int(previous.get("attempts") or 0) + 1,
            "lastFailedAt": utc_now(),
            "error": str(error),
        }

    def remove_pending(self, job_ids: Iterable[int]) -> None:
        for job_id in job_ids:
            self._pending_by_id.pop(int(job_id), None)

    def remove_pending_file_if_empty(self) -> None:
        if not self._pending_by_id and self.pending_path.exists():
            self.pending_path.unlink()

    def save(self) -> None:
        now = utc_now()
        self.pending["generatedAt"] = now
        self.pending["jobs"] = sorted(self._pending_by_id.values(), key=lambda job: int(job["id"]))
        self.state["lastRunAt"] = now
        self.state["seenIds"] = sorted(self._seen_ids)
        self._atomic_write(self.pending_path, self.pending)
        self._atomic_write(self.state_path, self.state)


class MadgexScanner:
    def __init__(
        self,
        client: MadgexClient,
        store: MadgexStore,
        *,
        workers: int = DEFAULT_WORKERS,
        checkpoint_every: int = 200,
        job_url_template: str = DEFAULT_JOB_URL,
        source_name: str = "Chronicle",
        minimum_posted_date: str = "",
    ) -> None:
        self.client = client
        self.store = store
        self.workers = max(1, min(32, int(workers)))
        self.checkpoint_every = max(1, int(checkpoint_every))
        self.job_url_template = str(job_url_template)
        self.source_name = str(source_name)
        self.minimum_posted_date = str(minimum_posted_date or "")

    @staticmethod
    def _report() -> Dict[str, int]:
        return {
            "sitemapJobs": 0,
            "requests": 0,
            "knownSkipped": 0,
            "queued": 0,
            "titleExcluded": 0,
            "dateExcluded": 0,
            "expired": 0,
            "missing": 0,
            "errors": 0,
        }

    def scan(self, *, bootstrap: bool = False) -> Dict[str, int]:
        report = self._report()
        sitemap_jobs = self.client.discover_jobs()
        report["sitemapJobs"] = len(sitemap_jobs)
        known = self.store.seen_ids
        # Use the stable numeric route after sitemap discovery. Some canonical
        # Madgex slugs contain Unicode characters that the board itself rejects
        # with HTTP 400, while /job/{id} resolves the same listing correctly.
        pending = [
            (job_id, self.job_url_template.format(job_id=job_id))
            for job_id in sorted(sitemap_jobs)
            if job_id not in known
        ]
        report["knownSkipped"] = len(sitemap_jobs) - len(pending)

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {
                executor.submit(self.client.fetch_job, job_id, url): job_id
                for job_id, url in pending
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                job_id = futures[future]
                result = future.result()
                report["requests"] += 1
                if result.kind == "job" and result.job:
                    posted_date = str(result.job.get("postedDate") or "")
                    if self.minimum_posted_date and (
                        not posted_date or posted_date < self.minimum_posted_date
                    ):
                        self.store.mark_seen(job_id)
                        report["dateExcluded"] += 1
                    elif not is_title_candidate(result.job.get("title")):
                        self.store.mark_seen(job_id)
                        report["titleExcluded"] += 1
                    else:
                        if self.store.add_job(result.job):
                            report["queued"] += 1
                elif result.kind == "expired":
                    self.store.mark_seen(job_id)
                    report["expired"] += 1
                elif result.kind == "not_found":
                    # Madgex sitemaps can briefly retain withdrawn listings.
                    # A plain 404 is a terminal inspection result, not a
                    # transient crawler failure, so do not retry it daily.
                    self.store.mark_seen(job_id)
                    report["missing"] += 1
                else:
                    report["errors"] += 1
                    self.store.record_error(job_id, result.error or result.kind)

                if completed % self.checkpoint_every == 0:
                    self.store.save()
                    print(
                        f"{self.source_name} checkpoint: inspected {completed}/{len(pending)} new sitemap jobs; "
                        f"queued {report['queued']}."
                    )

        self.store.state["lastSitemapCount"] = len(sitemap_jobs)
        self.store.state["lastSitemapMaxId"] = max(sitemap_jobs) if sitemap_jobs else None
        if bootstrap and report["errors"] == 0:
            self.store.state["bootstrapComplete"] = True
        self.store.save()
        return report


def raw_job_to_summary_input(
    job: Dict[str, Any],
    max_description_chars: int = MAX_DESCRIPTION_CHARS,
    job_url_template: str = DEFAULT_JOB_URL,
) -> Dict[str, Any]:
    description = str(job.get("description") or "").strip()
    if len(description) > max_description_chars:
        description = description[:max_description_chars].rstrip() + "…"
    return {
        "title": str(job.get("title") or ""),
        "company": str(job.get("institution") or ""),
        "location": str(job.get("location") or ""),
        "date_posted": str(job.get("postedDate") or ""),
        "description": description,
        "job_url": job_url_template.format(job_id=int(job["id"])),
        "source_id": str(job.get("id")),
    }
