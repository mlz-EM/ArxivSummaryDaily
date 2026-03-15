"""Paper summarization module using an LLM API."""

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from config.settings import QUERY
from .feed_utils import (
    date_sort_key,
    dedupe_incoming_by_url,
    extract_json_payload,
    load_existing_json_items,
    normalize_date,
    to_https_url,
    utc_generated_at,
)
from .llm_client import LLMModelClient


class PaperSummarizer:
    FEED_SOURCE = "TTAP Daily Feed"
    RETENTION_DAYS = 365

    def __init__(self, api_key: str, model: Optional[str] = None):
        self.client = LLMModelClient(api_key, model)
        self.max_papers_per_batch = 100

    def _filter_new_papers(self, papers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return dedupe_incoming_by_url(papers, "entry_id")

    def _mark_papers_processed(self, papers: List[Dict[str, Any]]):
        """No-op retained for backward compatibility with tests/integrations."""
        return

    def _utc_generated_at(self) -> str:
        return utc_generated_at()

    def _to_https_url(self, url: str) -> str:
        return to_https_url(url)

    def _normalize_date(self, raw: Any) -> str:
        return normalize_date(raw)

    def _paper_id_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        path = parsed.path
        m = re.search(r"/abs/([^/?#]+)", path)
        suffix = m.group(1) if m else (path.strip("/").split("/")[-1] if path else "item")
        suffix = re.sub(r"[^a-zA-Z0-9._-]", "-", suffix).strip("-") or "item"
        return f"arxiv-{suffix}".lower()

    def _extract_json_payload(self, content: str) -> List[Dict[str, Any]]:
        return extract_json_payload(content)

    def _generate_batch_summaries(self, papers: List[Dict[str, Any]], start_index: int) -> Tuple[List[Dict[str, Any]], bool]:
        batch_prompt_parts = []
        for i, paper in enumerate(papers, start=start_index):
            batch_prompt_parts.append(
                f"""
Paper {i}:
Title: {paper.get('title', '')}
Authors: {', '.join(paper.get('authors', []))}
Date: {self._normalize_date(paper.get('published', ''))}
arXiv link: {self._to_https_url(paper.get('entry_id', ''))}
Abstract: {paper.get('summary', '')}
"""
            )

        final_prompt = f"""Generate concise JSON summaries for the following {len(papers)} arXiv papers.
Rules:
1. Keep every provided paper.
2. Write one field called summary, maximum 65 words.
3. summary should include objective and key finding.
4. Output JSON only (no markdown, no explanation).
5. Output array items with keys exactly: url, summary.
6. url must match one of the provided arXiv links exactly.

Example:
[
  {{
    "url": "https://arxiv.org/abs/2603.11643v1",
    "summary": "Uses differential phase contrast STEM segmentation to recover nanoscale alloy microstructure and links local contrast to compositional variation."
  }}
]

Paper input:
{''.join(batch_prompt_parts)}"""

        try:
            response = self.client.chat_completion([{"role": "user", "content": final_prompt}])
            content = response["choices"][0]["message"]["content"].strip()
            parsed = self._extract_json_payload(content)
            print(f"Batch complete. Generated {len(parsed)} structured paper summaries...")
            return parsed, True
        except Exception as e:
            print(f"Batch generation failed: {e}")
            return [], False

    def _process_batch(self, papers: List[Dict[str, Any]], start_index: int) -> Tuple[List[Dict[str, Any]], bool]:
        print(f"Processing batch of {len(papers)} papers...")
        summaries, success = self._generate_batch_summaries(papers, start_index)
        time.sleep(2)
        return summaries, success

    def _generate_batch_summary(self, papers: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
        all_items: List[Dict[str, Any]] = []
        total_papers = len(papers)
        overall_success = True

        for i in range(0, total_papers, self.max_papers_per_batch):
            batch = papers[i : i + self.max_papers_per_batch]
            print(f"\nProcessing papers {i + 1} to {min(i + self.max_papers_per_batch, total_papers)}...")
            batch_items, ok = self._process_batch(batch, i + 1)
            all_items.extend(batch_items)
            overall_success = overall_success and ok
            if i + self.max_papers_per_batch < total_papers:
                print("Batch complete. Waiting 3 seconds...")
                time.sleep(3)

        return all_items, overall_success

    def _normalize_tags(self, categories: Any) -> List[str]:
        if not isinstance(categories, list):
            return []
        tags = []
        for cat in categories:
            text = str(cat).strip().lower()
            if not text:
                continue
            normalized = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
            if normalized:
                tags.append(normalized)
        return tags

    def _summary_fallback(self, abstract: str) -> str:
        sentence = (abstract or "").strip().replace("\n", " ")
        return sentence[:240].strip()

    def _normalize_paper_item(self, paper: Dict[str, Any], generated_summary: str) -> Optional[Dict[str, Any]]:
        url = self._to_https_url(paper.get("entry_id", ""))
        if not url or not url.startswith("https://"):
            return None

        summary = str(generated_summary or "").strip() or self._summary_fallback(paper.get("summary", ""))

        return {
            "id": self._paper_id_from_url(url),
            "title": str(paper.get("title") or "").strip(),
            "url": url,
            "date": self._normalize_date(paper.get("published")),
            "summary": summary,
            "tags": self._normalize_tags(paper.get("categories", [])),
        }

    def _load_existing_json_items(self, json_path: Path, retention_days: int = RETENTION_DAYS) -> List[Dict[str, Any]]:
        return load_existing_json_items(json_path, retention_days=retention_days)

    def _sort_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return sorted(items, key=lambda item: date_sort_key(item, "date"), reverse=True)

    def _build_header(self, item_count: int, generated_at: str, source: str) -> Dict[str, Any]:
        return {
            "title": "Basic Info",
            "model": self.client.model,
            "generatedAt": generated_at,
            "source": source,
            "notes": [
                f'This report includes the most recent {item_count} arXiv papers related to the keyword "{QUERY}".',
                "This page is powered by [ArxivSummaryDaily](https://github.com/dong-zehao/ArxivSummaryDaily).",
            ],
        }

    def summarize_papers(self, papers: List[Dict[str, Any]], output_file: str) -> bool:
        """Batch-process papers and update JSON feed only."""
        output_path = Path(output_file)
        output_json = output_path if output_path.suffix.lower() == ".json" else output_path.with_suffix(".json")

        try:
            existing_items = self._load_existing_json_items(output_json)
            existing_urls = {item.get("url") for item in existing_items if item.get("url")}

            candidate_new_papers = self._filter_new_papers(papers)
            new_papers = [
                paper
                for paper in candidate_new_papers
                if self._to_https_url(paper.get("entry_id", "")) not in existing_urls
            ]
            resurfaced_count = len(candidate_new_papers) - len(new_papers)
            if resurfaced_count:
                print(f"Skipping {resurfaced_count} papers already present in JSON feed.")

            summary_map: Dict[str, str] = {}
            api_success = True
            if new_papers:
                print(f"Generating summaries for {len(new_papers)} new papers...")
                raw_items, api_success = self._generate_batch_summary(new_papers)
                allowed_urls = {
                    self._to_https_url(p.get("entry_id", "")) for p in new_papers if p.get("entry_id")
                }
                for raw in raw_items:
                    url = self._to_https_url(str(raw.get("url") or "").strip())
                    if url in allowed_urls:
                        summary_map[url] = str(raw.get("summary") or "").strip()
            else:
                print("No new papers to summarize. Rebuilding feed from existing JSON data.")

            merged_by_url: Dict[str, Dict[str, Any]] = {
                self._to_https_url(item.get("url", "")): {
                    **item,
                    "url": self._to_https_url(item.get("url", "")),
                    "date": self._normalize_date(item.get("date")),
                    "tags": item.get("tags", []) if isinstance(item.get("tags", []), list) else [],
                }
                for item in existing_items
                if item.get("url")
            }

            for paper in new_papers:
                url = self._to_https_url(paper.get("entry_id", ""))
                normalized = self._normalize_paper_item(paper, summary_map.get(url, ""))
                if normalized:
                    merged_by_url[url] = normalized

            merged_items = self._sort_items(list(merged_by_url.values()))

            generated_at = self._utc_generated_at()
            feed_payload = {
                "header": self._build_header(len(merged_items), generated_at, self.FEED_SOURCE),
                "items": merged_items,
            }
            with open(output_json, "w", encoding="utf-8") as f:
                json.dump(feed_payload, f, indent=2, ensure_ascii=False)
            print(f"JSON feed updated: {output_json}")

            return api_success
        except Exception as e:
            print(f"Error generating paper feeds: {e}")
            return False
