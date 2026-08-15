"""Discover public Interfolio positions by numeric ID.

Position records remain in a temporary queue only until their first summary.
The durable scan state holds the numeric frontier and missing-ID observations,
so scheduled scans never need the historical bootstrap queue.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import threading
import time
from bisect import bisect_right
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

import requests


POSITION_URL = "https://logic.interfolio.com/dossier-api/positions/{position_id}"
LANDING_URL = "https://apply.interfolio.com/{position_id}"
DEFAULT_START_ID = 180000
DEFAULT_LOOKAHEAD = 400
DEFAULT_RECENT_DAYS = 7

def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_interfolio_date(value: Any) -> str:
    """Normalize Interfolio's ISO or ``Mon DD, YYYY`` date formats."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    return match.group(1) if match else ""


class _TextExtractor(HTMLParser):
    BLOCK_TAGS = {
        "br",
        "div",
        "li",
        "ol",
        "p",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Any]) -> None:
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: Any) -> str:
    if value is None:
        return ""
    parser = _TextExtractor()
    parser.feed(str(value))
    parser.close()
    text = html.unescape("".join(parser.parts)).replace("\xa0", " ")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


@dataclass(frozen=True)
class FetchResult:
    kind: str
    status_code: Optional[int] = None
    position: Optional[Dict[str, Any]] = None
    error: str = ""


class InterfolioClient:
    """Small rate-limited client for Interfolio's public position endpoints."""

    def __init__(
        self,
        *,
        session: Optional[requests.Session] = None,
        request_delay: float = 0.5,
        timeout: float = 30,
        retry_count: int = 4,
        retry_delay: float = 2,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.session = session
        self._session_local = threading.local()
        self.request_delay = max(0.0, request_delay)
        self.timeout = timeout
        self.retry_count = max(1, retry_count)
        self.retry_delay = max(0.0, retry_delay)
        self.sleep = sleep
        self._last_request_at: Optional[float] = None
        self._request_start_lock = threading.Lock()
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://apply.interfolio.com",
            "User-Agent": "ArxivSummaryDaily/0.1 (personal academic job monitor)",
        }

    def _throttle(self) -> None:
        # Space request starts globally while still allowing response waits to
        # overlap when the scanner uses a small worker pool.
        with self._request_start_lock:
            if self._last_request_at is not None and self.request_delay:
                elapsed = time.monotonic() - self._last_request_at
                if elapsed < self.request_delay:
                    self.sleep(self.request_delay - elapsed)
            self._last_request_at = time.monotonic()

    def _get(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> requests.Response:
        self._throttle()
        session = self.session
        if session is None:
            session = getattr(self._session_local, "session", None)
            if session is None:
                session = requests.Session()
                self._session_local.session = session
        return session.get(
            url,
            params=params,
            headers=self.headers,
            timeout=self.timeout if timeout is None else timeout,
        )

    def _retry_wait(self, response: Optional[requests.Response], attempt: int) -> None:
        retry_after = "" if response is None else response.headers.get("Retry-After", "")
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            delay = self.retry_delay * (2**attempt)
        if delay:
            self.sleep(delay)

    def fetch_position(self, position_id: int) -> FetchResult:
        url = POSITION_URL.format(position_id=int(position_id))
        last_error = ""
        last_status: Optional[int] = None

        for attempt in range(self.retry_count):
            response: Optional[requests.Response] = None
            try:
                response = self._get(url)
                last_status = response.status_code
                if response.status_code == 404:
                    return FetchResult("not_found", status_code=404)
                if response.status_code in {401, 403}:
                    return FetchResult("private", status_code=response.status_code)
                if response.status_code in {400, 410, 422}:
                    return FetchResult("unavailable", status_code=response.status_code)
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}"
                    if attempt + 1 < self.retry_count:
                        self._retry_wait(response, attempt)
                        continue
                    break
                if response.status_code != 200:
                    return FetchResult(
                        "error",
                        status_code=response.status_code,
                        error=f"unexpected HTTP {response.status_code}",
                    )

                payload = response.json()
                if not isinstance(payload, dict) or not payload.get("position_id"):
                    return FetchResult("empty", status_code=200)
                return FetchResult("position", status_code=200, position=payload)
            except (requests.RequestException, ValueError) as exc:
                last_error = str(exc)
                if attempt + 1 < self.retry_count:
                    self._retry_wait(response, attempt)
                    continue

        return FetchResult("error", status_code=last_status, error=last_error or "request failed")

def normalize_position(payload: Dict[str, Any], observed_at: Optional[str] = None) -> Dict[str, Any]:
    """Convert a public position response into the append-only raw schema."""
    position_id = int(payload["position_id"])
    observed_at = observed_at or utc_now()
    record = {
        "id": position_id,
        "tenantId": payload.get("tenant_id"),
        "title": str(payload.get("position_name") or "").strip(),
        "institution": str(payload.get("institution_condensed") or payload.get("institution") or "").strip(),
        "location": str(payload.get("location") or "").strip(),
        "postedDate": normalize_interfolio_date(payload.get("start_date")),
        "deadline": normalize_interfolio_date(payload.get("end_date")),
        "isOpen": bool(payload.get("is_open")),
        "isClosed": bool(payload.get("is_closed")),
        "activeStatus": str(payload.get("active_status") or "").strip(),
        "description": html_to_text(payload.get("landing_page_description")),
        "firstSeenAt": observed_at,
        "lastSeenAt": observed_at,
    }
    hash_payload = {key: value for key, value in record.items() if key not in {"firstSeenAt", "lastSeenAt"}}
    serialized = json.dumps(hash_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    record["contentHash"] = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return record


def canonical_school_name(institution: Any) -> str:
    """Reduce Interfolio's ``School: Department`` label to the school name."""
    return str(institution or "").split(":", 1)[0].strip()


def build_tenant_directory(jobs: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """Build a stable tenant-ID-to-school mapping from normalized queued jobs."""
    names: Dict[int, Counter[str]] = defaultdict(Counter)
    for job in jobs:
        try:
            tenant_id = int(job.get("tenantId"))
        except (TypeError, ValueError):
            continue
        school = canonical_school_name(job.get("institution"))
        if school:
            names[tenant_id][school] += 1

    directory: Dict[str, str] = {}
    for tenant_id in sorted(names):
        # Prefer the most common label; lexical order makes ties deterministic.
        school = sorted(names[tenant_id].items(), key=lambda item: (-item[1], item[0]))[0][0]
        directory[str(tenant_id)] = school
    return directory


def save_tenant_directory(path: Path, jobs: Iterable[Dict[str, Any]]) -> bool:
    """Merge newly observed tenant names into the durable school directory."""
    path = Path(path)
    tenants = build_tenant_directory(jobs)
    previous: Dict[str, Any] = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                previous = loaded
        except (OSError, json.JSONDecodeError):
            pass
    merged = dict(previous.get("tenants") or {})
    merged.update(tenants)
    if previous.get("tenants") == merged:
        return False
    InterfolioStore._atomic_write(
        path,
        {
            "schemaVersion": 1,
            "generatedAt": utc_now(),
            "tenants": dict(sorted(merged.items(), key=lambda item: int(item[0]))),
        },
    )
    return True


class InterfolioStore:
    """Temporary unsummarized-job queue plus durable mutable crawl state."""

    def __init__(self, raw_path: Path, state_path: Path):
        self.raw_path = Path(raw_path)
        self.state_path = Path(state_path)
        self.raw = self._load_json(
            self.raw_path,
            {"schemaVersion": 1, "generatedAt": "", "jobs": []},
        )
        self.state = self._load_json(
            self.state_path,
            {
                "schemaVersion": 2,
                "startId": DEFAULT_START_ID,
                "bootstrapComplete": False,
                "bootstrapNextId": DEFAULT_START_ID,
                "highestDiscoveredId": None,
                "lastScannedId": None,
                "lastRunAt": "",
                "missing": {},
                "completedMissingIds": [],
                "pendingErrors": {},
            },
        )
        if not isinstance(self.raw.get("jobs"), list):
            self.raw["jobs"] = []
        if not isinstance(self.state.get("missing"), dict):
            self.state["missing"] = {}
        if not isinstance(self.state.get("pendingErrors"), dict):
            self.state["pendingErrors"] = {}
        if not isinstance(self.state.get("completedMissingIds"), list):
            self.state["completedMissingIds"] = []
        self.state["schemaVersion"] = 2
        self._completed_missing_ids: Set[int] = {
            int(value)
            for value in self.state["completedMissingIds"]
            if str(value).isdigit()
        }
        self._jobs_by_id: Dict[int, Dict[str, Any]] = {
            int(job["id"]): job
            for job in self.raw["jobs"]
            if isinstance(job, dict) and str(job.get("id", "")).isdigit()
        }
        if self._jobs_by_id:
            actual_highest = max(self._jobs_by_id)
            saved_highest = self.state.get("highestDiscoveredId")
            self.state["highestDiscoveredId"] = max(actual_highest, int(saved_highest or 0))

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
        temporary = path.with_suffix(path.suffix + ".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        temporary.replace(path)

    @property
    def known_ids(self) -> Set[int]:
        return set(self._jobs_by_id)

    @property
    def jobs(self) -> List[Dict[str, Any]]:
        return list(self._jobs_by_id.values())

    @property
    def completed_missing_ids(self) -> Set[int]:
        return set(self._completed_missing_ids)

    def add_position(self, payload: Dict[str, Any], observed_at: Optional[str] = None) -> bool:
        position_id = int(payload["position_id"])
        if position_id in self._jobs_by_id:
            return False
        record = normalize_position(payload, observed_at=observed_at)
        self._jobs_by_id[position_id] = record
        self.state["highestDiscoveredId"] = max(
            position_id,
            int(self.state.get("highestDiscoveredId") or 0),
        )
        self.state["missing"].pop(str(position_id), None)
        self._completed_missing_ids.discard(position_id)
        self.state["pendingErrors"].pop(str(position_id), None)
        posted_date = str(record.get("postedDate") or "")
        if posted_date:
            for raw_id, observation in self.state["missing"].items():
                missing_id = int(raw_id)
                try:
                    next_larger_id = int(observation.get("nextLargerId"))
                except (TypeError, ValueError):
                    continue
                if missing_id < position_id < next_larger_id:
                    observation["nextLargerId"] = position_id
                    observation["nextLargerPostedDate"] = posted_date
        return True

    def record_missing(
        self,
        position_id: int,
        kind: str,
        checked_at: Optional[str] = None,
        *,
        next_larger_id: Optional[int] = None,
        next_larger_posted_date: str = "",
    ) -> None:
        position_id = int(position_id)
        if position_id in self._jobs_by_id or position_id in self._completed_missing_ids:
            return
        checked_at = checked_at or utc_now()
        key = str(position_id)
        existing = self.state["missing"].get(key, {})
        observation = {
            "kind": kind,
            "firstCheckedAt": existing.get("firstCheckedAt") or checked_at,
            "lastCheckedAt": checked_at,
        }
        if next_larger_id is not None:
            observation["nextLargerId"] = int(next_larger_id)
            observation["nextLargerPostedDate"] = normalize_interfolio_date(
                next_larger_posted_date
            )
        else:
            if existing.get("nextLargerId") is not None:
                observation["nextLargerId"] = existing["nextLargerId"]
            if existing.get("nextLargerPostedDate"):
                observation["nextLargerPostedDate"] = existing["nextLargerPostedDate"]
        self.state["missing"][key] = observation
        self.state["pendingErrors"].pop(key, None)

    def record_error(self, position_id: int, message: str, checked_at: Optional[str] = None) -> None:
        if int(position_id) in self._jobs_by_id:
            return
        checked_at = checked_at or utc_now()
        key = str(int(position_id))
        existing = self.state["pendingErrors"].get(key, {})
        self.state["pendingErrors"][key] = {
            "firstFailedAt": existing.get("firstFailedAt") or checked_at,
            "lastFailedAt": checked_at,
            "attempts": int(existing.get("attempts") or 0) + 1,
            "error": message,
        }

    def retryable_missing_ids(
        self,
        recent_days: int,
        today: Optional[date] = None,
    ) -> List[int]:
        today = today or datetime.now(timezone.utc).date()
        cutoff = today - timedelta(days=max(1, int(recent_days)))
        retryable: List[int] = []
        completed: List[int] = []
        for raw_id, observation in list(self.state["missing"].items()):
            posted = normalize_interfolio_date(observation.get("nextLargerPostedDate"))
            if not posted:
                observation["status"] = "unresolved"
                retryable.append(int(raw_id))
                continue
            try:
                posted_date = date.fromisoformat(posted)
            except ValueError:
                observation["status"] = "unresolved"
                retryable.append(int(raw_id))
                continue
            if posted_date < cutoff:
                completed.append(int(raw_id))
            elif posted_date > today:
                observation["status"] = "deferred"
            else:
                observation["status"] = "retry"
                retryable.append(int(raw_id))
        for position_id in completed:
            self.state["missing"].pop(str(position_id), None)
            self._completed_missing_ids.add(position_id)
        return sorted(retryable)

    def drop_missing_above(self, highest_discovered_id: int) -> None:
        highest_discovered_id = int(highest_discovered_id)
        for raw_id in list(self.state["missing"]):
            if int(raw_id) > highest_discovered_id:
                self.state["missing"].pop(raw_id, None)
        for raw_id in list(self.state["pendingErrors"]):
            if int(raw_id) > highest_discovered_id:
                self.state["pendingErrors"].pop(raw_id, None)

    def annotate_missing_with_nearest_positions(self) -> None:
        position_ids = sorted(self._jobs_by_id)
        for raw_id, observation in self.state["missing"].items():
            missing_id = int(raw_id)
            next_index = bisect_right(position_ids, missing_id)
            if next_index >= len(position_ids):
                continue
            next_larger_id = position_ids[next_index]
            observation["nextLargerId"] = next_larger_id
            observation["nextLargerPostedDate"] = str(
                self._jobs_by_id[next_larger_id].get("postedDate") or ""
            )

    def save(self) -> None:
        now = utc_now()
        self.raw["generatedAt"] = now
        self.raw["jobs"] = sorted(self._jobs_by_id.values(), key=lambda job: int(job["id"]))
        self.state["lastRunAt"] = now
        self.state["completedMissingIds"] = sorted(self._completed_missing_ids)
        self._atomic_write(self.raw_path, self.raw)
        self._atomic_write(self.state_path, self.state)


class InterfolioScanner:
    """Resumable bootstrap and daily discovery without refreshing known jobs."""

    def __init__(
        self,
        client: InterfolioClient,
        store: InterfolioStore,
        *,
        start_id: int = DEFAULT_START_ID,
        lookahead: int = DEFAULT_LOOKAHEAD,
        recent_days: int = DEFAULT_RECENT_DAYS,
        checkpoint_every: int = 50,
        workers: int = 1,
    ):
        self.client = client
        self.store = store
        self.start_id = int(start_id)
        self.lookahead = max(1, int(lookahead))
        self.recent_days = max(1, int(recent_days))
        self.checkpoint_every = max(1, int(checkpoint_every))
        self.workers = max(1, min(64, int(workers)))
        self.store.state["startId"] = self.start_id
        self._frontier_trailing: Dict[int, FetchResult] = {}
        self._frontier_positions: Dict[int, str] = {}

    def _process_result(
        self,
        position_id: int,
        result: FetchResult,
        report: Dict[str, int],
        *,
        persist_non_position: bool = True,
        next_larger_id: Optional[int] = None,
        next_larger_posted_date: str = "",
    ) -> None:
        report["requests"] += 1
        if result.kind == "position" and result.position:
            if self.store.add_position(result.position):
                report["newJobs"] += 1
            return
        if result.kind in {"empty", "not_found", "private", "unavailable"}:
            if persist_non_position:
                self.store.record_missing(
                    position_id,
                    result.kind,
                    next_larger_id=next_larger_id,
                    next_larger_posted_date=next_larger_posted_date,
                )
            report["missing"] += 1
            return
        if persist_non_position:
            self.store.record_error(position_id, result.error or result.kind)
        report["errors"] += 1

    def _fetch_new_id(self, position_id: int, report: Dict[str, int]) -> None:
        if position_id in self.store.known_ids:
            report["knownSkipped"] += 1
            return
        self._process_result(position_id, self.client.fetch_position(position_id), report)

    def _fetch_ids(
        self,
        requested_ids: Iterable[int],
        report: Dict[str, int],
        *,
        frontier: bool = False,
    ) -> None:
        position_ids: List[int] = []
        known_ids = self.store.known_ids
        for position_id in requested_ids:
            if position_id in known_ids:
                report["knownSkipped"] += 1
            else:
                position_ids.append(position_id)

        if self.workers == 1:
            results = [self.client.fetch_position(position_id) for position_id in position_ids]
        else:
            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                results = list(executor.map(self.client.fetch_position, position_ids))
        if not frontier:
            for position_id, result in zip(position_ids, results):
                self._process_result(position_id, result, report)
            return

        result_pairs = list(zip(position_ids, results))
        for position_id, result in result_pairs:
            if result.kind == "position" and result.position:
                self._frontier_positions[position_id] = normalize_interfolio_date(
                    result.position.get("start_date")
                )
                self._process_result(position_id, result, report)
            else:
                self._frontier_trailing[position_id] = result
                self._process_result(
                    position_id,
                    result,
                    report,
                    persist_non_position=False,
                )

        highest = int(self.store.state.get("highestDiscoveredId") or (self.start_id - 1))
        frontier_position_ids = sorted(self._frontier_positions)
        for position_id in sorted(self._frontier_trailing):
            if position_id > highest:
                continue
            result = self._frontier_trailing[position_id]
            next_index = bisect_right(frontier_position_ids, position_id)
            next_larger_id = (
                frontier_position_ids[next_index]
                if next_index < len(frontier_position_ids)
                else None
            )
            if result.kind in {"empty", "not_found", "private", "unavailable"}:
                self.store.record_missing(
                    position_id,
                    result.kind,
                    next_larger_id=next_larger_id,
                    next_larger_posted_date=(
                        self._frontier_positions.get(next_larger_id, "")
                        if next_larger_id is not None
                        else ""
                    ),
                )
            else:
                self.store.record_error(position_id, result.error or result.kind)
            self._frontier_trailing.pop(position_id, None)

    def _fetch_range(
        self,
        first_id: int,
        last_id: int,
        report: Dict[str, int],
        *,
        frontier: bool = False,
    ) -> None:
        self._fetch_ids(range(first_id, last_id + 1), report, frontier=frontier)

    @staticmethod
    def _report() -> Dict[str, int]:
        return {
            "requests": 0,
            "newJobs": 0,
            "knownSkipped": 0,
            "missing": 0,
            "errors": 0,
        }

    def _retry_pending_errors(self, report: Dict[str, int]) -> None:
        pending_ids = sorted(int(raw_id) for raw_id in self.store.state["pendingErrors"])
        for position_id in pending_ids:
            self._fetch_new_id(position_id, report)

    def bootstrap(self) -> Dict[str, int]:
        report = self._report()
        self.store.state["bootstrapComplete"] = False
        self._retry_pending_errors(report)

        current = max(self.start_id, int(self.store.state.get("bootstrapNextId") or self.start_id))
        highest = int(self.store.state.get("highestDiscoveredId") or 0)
        target = max(self.start_id + self.lookahead - 1, highest + self.lookahead)

        while current <= target:
            batch_end = min(target, current + self.checkpoint_every - 1)
            self._fetch_range(current, batch_end, report)
            highest = int(self.store.state.get("highestDiscoveredId") or 0)
            target = max(target, highest + self.lookahead)
            self.store.state["bootstrapNextId"] = batch_end + 1
            self.store.state["lastScannedId"] = batch_end
            self.store.save()
            print(
                f"Interfolio bootstrap checkpoint: scanned through {batch_end}; "
                f"stored {len(self.store.jobs)} jobs."
            )
            current = batch_end + 1

        self._retry_pending_errors(report)
        highest = int(self.store.state.get("highestDiscoveredId") or 0)
        self.store.drop_missing_above(highest)
        self.store.annotate_missing_with_nearest_positions()
        self.store.state["scanUpperBound"] = target
        self.store.state["bootstrapComplete"] = not bool(self.store.state["pendingErrors"])
        self.store.save()
        return report

    def daily(self) -> Dict[str, int]:
        if not self.store.state.get("bootstrapComplete"):
            raise RuntimeError("initial Interfolio bootstrap is incomplete; run scan --mode bootstrap first")

        report = self._report()
        frontier_start = max(
            self.start_id,
            int(self.store.state.get("highestDiscoveredId") or (self.start_id - 1)) + 1,
        )
        self._retry_pending_errors(report)

        highest_before_retry = int(
            self.store.state.get("highestDiscoveredId") or (self.start_id - 1)
        )
        # IDs above the high-water mark are covered by the frontier pass below;
        # retry only historical holes here to avoid duplicate requests.
        retry_ids = [
            position_id
            for position_id in self.store.retryable_missing_ids(self.recent_days)
            if position_id <= highest_before_retry
        ]
        for offset in range(0, len(retry_ids), self.checkpoint_every):
            self._fetch_ids(retry_ids[offset : offset + self.checkpoint_every], report)
            self.store.save()
            print(
                f"Interfolio historical retry checkpoint: inspected "
                f"{min(offset + self.checkpoint_every, len(retry_ids))}/{len(retry_ids)} IDs."
            )

        highest_before_frontier = int(self.store.state.get("highestDiscoveredId") or (self.start_id - 1))
        current = frontier_start
        target = highest_before_frontier + self.lookahead
        while current <= target:
            batch_end = min(target, current + self.checkpoint_every - 1)
            self._fetch_range(current, batch_end, report, frontier=True)
            highest = int(self.store.state.get("highestDiscoveredId") or highest_before_frontier)
            target = max(target, highest + self.lookahead)
            self.store.state["lastScannedId"] = batch_end
            self.store.save()
            print(
                f"Interfolio daily frontier checkpoint: scanned through {batch_end}; "
                f"stored {len(self.store.jobs)} jobs."
            )
            current = batch_end + 1

        highest = int(self.store.state.get("highestDiscoveredId") or highest_before_frontier)
        self.store.drop_missing_above(highest)
        self.store.state["scanUpperBound"] = target
        self.store.save()
        return report


def raw_job_to_summary_input(job: Dict[str, Any], max_description_chars: int = 16000) -> Dict[str, Any]:
    description = str(job.get("description") or "").strip()
    if len(description) > max_description_chars:
        description = description[:max_description_chars].rstrip() + "…"
    return {
        "title": str(job.get("title") or ""),
        "company": str(job.get("institution") or ""),
        "location": str(job.get("location") or ""),
        "date_posted": str(job.get("postedDate") or ""),
        "description": description,
        "job_url": LANDING_URL.format(position_id=job["id"]),
        "source_id": str(job.get("id")),
    }


def is_summary_candidate(job: Dict[str, Any]) -> bool:
    """Keep open, faculty-like titles after applying shared exclusions."""
    if job.get("isClosed") is True:
        return False
    if "closed" in str(job.get("activeStatus") or "").lower():
        return False
    title = str(job.get("title") or "").lower()
    exclude = (
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
    if any(term in title for term in exclude):
        return False
    if re.search(r"\bnon[\s-]+tenure[\s-]+track\b", title):
        return False
    return bool(
        re.search(
            r"\bprofessor\b|\bfaculty\b|\bopen[\s-]+rank\b|\btenure[\s-]+track\b",
            title,
        )
    )


def load_processed_ids(path: Path) -> Set[int]:
    if not path.exists():
        return set()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return {int(value) for value in payload.get("processedIds", [])}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return set()


def save_processed_ids(path: Path, processed_ids: Iterable[int]) -> None:
    payload = {
        "schemaVersion": 1,
        "updatedAt": utc_now(),
        "processedIds": sorted({int(value) for value in processed_ids}),
    }
    InterfolioStore._atomic_write(Path(path), payload)
