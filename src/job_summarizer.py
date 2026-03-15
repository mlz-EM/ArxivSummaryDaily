"""Job summarization module using an LLM API."""

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .feed_utils import (
    date_sort_key,
    dedupe_incoming_by_url,
    extract_json_payload,
    load_existing_json_items,
    normalize_date,
    prune_items_by_retention,
    to_https_url,
    utc_generated_at,
)
from .llm_client import LLMModelClient


class JobSummarizer:
    FEED_SOURCE = "TTAP Daily Feed"
    RETENTION_DAYS = 365

    def __init__(self, api_key: str, model: Optional[str] = None):
        self.client = LLMModelClient(api_key, model)
        self.max_papers_per_batch = 100

    def _filter_new_jobs(self, jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return dedupe_incoming_by_url(jobs, "job_url")

    def _mark_jobs_processed(self, jobs: List[Dict[str, Any]]):
        """No-op retained for backward compatibility with tests/integrations."""
        return

    def _utc_generated_at(self) -> str:
        return utc_generated_at()

    def _to_https_url(self, url: str) -> str:
        return to_https_url(url)

    def _normalize_date(self, raw: Any) -> str:
        return normalize_date(raw)

    def _job_id_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        host_prefix = (parsed.netloc.split(".")[0] if parsed.netloc else "job") or "job"
        segments = [seg for seg in parsed.path.split("/") if seg]
        tail = segments[-1] if segments else "item"
        tail = tail.split("?")[0]
        tail = re.sub(r"[^a-zA-Z0-9._-]", "-", tail).strip("-") or "item"
        return f"{host_prefix}-{tail}".lower()

    def _extract_json_payload(self, content: str) -> List[Dict[str, Any]]:
        return extract_json_payload(content)

    def _generate_batch_summaries(self, jobs: List[Dict[str, Any]], start_index: int) -> Tuple[List[Dict[str, Any]], bool]:
        batch_prompt_parts = []
        for i, job in enumerate(jobs, start=start_index):
            batch_prompt_parts.append(
                f"""
job {i}:
title: {job.get('title', '')}
school: {job.get('company', '')}
location: {job.get('location', '')}
posted: {job.get('date_posted', '')}
description: {job.get('description', '')}
job_url: {job.get('job_url', '')}
"""
            )

        final_prompt = f"""I am a PhD graduate in Materials Engineering focused on electron microscopy and structure-property relationships.
Filter and summarize the following {len(jobs)} tenure-track job opportunities.
Rules:
1. Remove jobs that are clearly unrelated (e.g., humanities, medical school, administrative roles).
2. Remove jobs that are not tenure-track (e.g., adjunct, teaching faculty only).
3. Remove jobs where the institution is not R1.
4. For remaining jobs, score fitScore from 1 to 3 (integer only).
5. Description must be one concise sentence under 20 words.
6. Output JSON only (no markdown, no explanation).
7. Output array items with keys exactly:
   title, url, date, location, description, fitScore, keywords
8. date must be YYYY-MM-DD.
9. url must match one of the provided job_url values exactly.
10. keywords must be an array (use [] when unknown).

Example output:
[
  {{
    "title": "Assistant Professor of Materials Science",
    "url": "https://www.linkedin.com/jobs/view/123",
    "date": "2026-03-11",
    "location": "Example University at Example City, ST",
    "description": "Seeks tenure-track faculty in advanced materials characterization.",
    "fitScore": 3,
    "keywords": ["electron microscopy", "materials characterization"]
  }}
]

Job input:
{''.join(batch_prompt_parts)}"""

        try:
            response = self.client.chat_completion([{"role": "user", "content": final_prompt}])
            content = response["choices"][0]["message"]["content"].strip()
            parsed = self._extract_json_payload(content)
            print(f"Batch complete. Generated {len(parsed)} structured job summaries...")
            return parsed, True
        except Exception as e:
            print(f"Batch generation failed: {e}")
            return [], False

    def _process_batch(self, jobs: List[Dict[str, Any]], start_index: int) -> Tuple[List[Dict[str, Any]], bool]:
        print(f"Processing batch of {len(jobs)} jobs...")
        summaries, success = self._generate_batch_summaries(jobs, start_index)
        time.sleep(2)
        return summaries, success

    def _generate_batch_summary(self, jobs: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
        all_items: List[Dict[str, Any]] = []
        total_jobs = len(jobs)
        overall_success = True

        for i in range(0, total_jobs, self.max_papers_per_batch):
            batch = jobs[i : i + self.max_papers_per_batch]
            print(f"\nProcessing jobs {i + 1} to {min(i + self.max_papers_per_batch, total_jobs)}...")
            batch_items, ok = self._process_batch(batch, i + 1)
            all_items.extend(batch_items)
            overall_success = overall_success and ok
            if i + self.max_papers_per_batch < total_jobs:
                print("Batch complete. Waiting 3 seconds...")
                time.sleep(3)

        return all_items, overall_success

    def _normalize_job_item(self, raw: Dict[str, Any], is_new: bool) -> Optional[Dict[str, Any]]:
        url = self._to_https_url(str(raw.get("url") or raw.get("job_url") or "").strip())
        if not url or not url.startswith("https://"):
            return None

        fit_score_raw = raw.get("fitScore", raw.get("fit_score", 1))
        try:
            fit_score = int(fit_score_raw)
        except (TypeError, ValueError):
            fit_score = 1
        fit_score = max(1, min(3, fit_score))

        keywords = raw.get("keywords", [])
        if not isinstance(keywords, list):
            keywords = []

        date = self._normalize_date(raw.get("date"))

        return {
            "id": self._job_id_from_url(url),
            "title": str(raw.get("title") or "").strip(),
            "url": url,
            "date": date,
            "location": str(raw.get("location") or "").strip(),
            "description": str(raw.get("description") or "").strip(),
            "fitScore": fit_score,
            "isNew": bool(is_new),
            "keywords": [str(k).strip() for k in keywords if str(k).strip()],
        }

    def _load_existing_json_items(self, json_path: Path, retention_days: int = RETENTION_DAYS) -> List[Dict[str, Any]]:
        return load_existing_json_items(json_path, retention_days=retention_days)

    def _sort_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        new_items = [item for item in items if item.get("isNew")]
        old_items = [item for item in items if not item.get("isNew")]
        new_items.sort(key=lambda item: date_sort_key(item, "date"), reverse=True)
        old_items.sort(key=lambda item: date_sort_key(item, "date"), reverse=True)
        return new_items + old_items

    def _build_header(self, generated_at: str, source: str) -> Dict[str, Any]:
        return {
            "title": "Basic Info",
            "model": self.client.model,
            "generatedAt": generated_at,
            "source": source,
            "notes": [
                "This report includes recent tenure-track engineering-related positions from Google, LinkedIn and Indeed.",
                "This page is powered by [ArxivSummaryDaily](https://github.com/dong-zehao/ArxivSummaryDaily) and [JobSpy](https://github.com/speedyapply/JobSpy).",
            ],
        }

    def _build_rejected_job_item(self, job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Persist AI-rejected jobs to avoid sending them again in future runs."""
        url = self._to_https_url(job.get("job_url", ""))
        if not url or not url.startswith("https://"):
            return None

        location = str(job.get("location") or "").strip()
        company = str(job.get("company") or "").strip()
        if company and location:
            location = f"{company} at {location}"

        return {
            "id": self._job_id_from_url(url),
            "title": str(job.get("title") or "").strip(),
            "url": url,
            "date": self._normalize_date(job.get("date_posted")),
            "location": location,
            "description": "",
            "fitScore": 0,
            "isNew": True,
            "keywords": [],
        }

    def summarize_jobs(self, jobs: List[Dict[str, Any]], output_file: str) -> bool:
        """Batch-process jobs and update JSON feed only."""
        output_path = Path(output_file)
        output_json = output_path if output_path.suffix.lower() == ".json" else output_path.with_suffix(".json")

        try:
            existing_items = self._load_existing_json_items(output_json)
            existing_urls = {item.get("url") for item in existing_items if item.get("url")}

            candidate_new_jobs = self._filter_new_jobs(jobs)
            new_jobs = [job for job in candidate_new_jobs if self._to_https_url(job.get("job_url", "")) not in existing_urls]
            resurfaced_count = len(candidate_new_jobs) - len(new_jobs)
            if resurfaced_count:
                print(f"Skipping {resurfaced_count} jobs already present in JSON feed.")

            new_items_raw: List[Dict[str, Any]] = []
            api_success = True
            if new_jobs:
                print(f"Generating summaries for {len(new_jobs)} new jobs...")
                new_items_raw, api_success = self._generate_batch_summary(new_jobs)
            else:
                print("No new jobs to summarize. Rebuilding feed from existing JSON data.")

            allowed_urls = {self._to_https_url(job.get("job_url", "")) for job in new_jobs if job.get("job_url")}
            normalized_new_items: List[Dict[str, Any]] = []
            for raw in new_items_raw:
                normalized = self._normalize_job_item(raw, is_new=True)
                if not normalized:
                    continue
                if normalized["url"] not in allowed_urls:
                    continue
                normalized_new_items.append(normalized)

            returned_urls = {item["url"] for item in normalized_new_items}
            for job in new_jobs:
                job_url = self._to_https_url(job.get("job_url", ""))
                if not job_url or job_url in returned_urls:
                    continue
                rejected_item = self._build_rejected_job_item(job)
                if rejected_item:
                    normalized_new_items.append(rejected_item)

            merged_by_url: Dict[str, Dict[str, Any]] = {
                item["url"]: {
                    **item,
                    "url": self._to_https_url(item.get("url", "")),
                    "isNew": False,
                    "fitScore": max(0, min(3, int(item.get("fitScore", 0) or 0))),
                    "keywords": item.get("keywords", []) if isinstance(item.get("keywords", []), list) else [],
                    "date": self._normalize_date(item.get("date")),
                }
                for item in existing_items
                if item.get("url")
            }

            for item in normalized_new_items:
                merged_by_url[item["url"]] = item

            merged_items = list(merged_by_url.values())
            merged_items = self._load_existing_json_items_from_list(merged_items)
            merged_items = self._sort_items(merged_items)

            generated_at = self._utc_generated_at()
            feed_payload = {
                "header": self._build_header(generated_at, self.FEED_SOURCE),
                "items": merged_items,
            }
            with open(output_json, "w", encoding="utf-8") as f:
                json.dump(feed_payload, f, indent=2, ensure_ascii=False)
            print(f"JSON feed updated: {output_json}")

            return api_success
        except Exception as e:
            print(f"Error generating job feeds: {e}")
            return False

    def _load_existing_json_items_from_list(self, items: List[Dict[str, Any]], retention_days: int = RETENTION_DAYS) -> List[Dict[str, Any]]:
        return prune_items_by_retention(items, retention_days=retention_days, date_key="date")
