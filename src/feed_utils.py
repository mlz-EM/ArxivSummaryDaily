"""Shared utilities for JSON feed parsing and normalization."""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List


def utc_generated_at() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def to_https_url(url: Any) -> str:
    if not url:
        return ""
    normalized = str(url).strip()
    if normalized.startswith("http://"):
        return "https://" + normalized[len("http://") :]
    return normalized


def normalize_date(raw: Any) -> str:
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return text
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
        return m.group(1) if m else ""


def extract_json_payload(content: str) -> List[Dict[str, Any]]:
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            raise
        payload = json.loads(text[start : end + 1])

    if isinstance(payload, dict):
        items = payload.get("items", [])
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def dedupe_incoming_by_url(items: List[Dict[str, Any]], url_key: str) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen_urls = set()
    for item in items:
        url = to_https_url(item.get(url_key, ""))
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        deduped.append(item)
    return deduped


def load_existing_json_items(json_path: Path, retention_days: int = 365) -> List[Dict[str, Any]]:
    if not json_path.exists():
        return []
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        items = payload.get("items", []) if isinstance(payload, dict) else []
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(items, list):
        return []

    return prune_items_by_retention(items, retention_days=retention_days, date_key="date")


def prune_items_by_retention(
    items: List[Dict[str, Any]],
    retention_days: int = 365,
    date_key: str = "date",
) -> List[Dict[str, Any]]:
    cutoff = datetime.now().date() - timedelta(days=retention_days)
    filtered: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        date_str = normalize_date(item.get(date_key))
        item[date_key] = date_str
        if not date_str:
            filtered.append(item)
            continue
        try:
            item_date = datetime.fromisoformat(date_str).date()
        except ValueError:
            filtered.append(item)
            continue
        if item_date >= cutoff:
            filtered.append(item)
    return filtered


def date_sort_key(item: Dict[str, Any], date_key: str = "date"):
    date_str = normalize_date(item.get(date_key))
    if not date_str:
        return (False, datetime.min.date())
    try:
        return (True, datetime.fromisoformat(date_str).date())
    except ValueError:
        return (False, datetime.min.date())
