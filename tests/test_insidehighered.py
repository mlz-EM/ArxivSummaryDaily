"""Tests for the Inside Higher Ed wrapper around shared Madgex discovery."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.insidehighered_client import (
    FetchResult,
    InsideHigherEdScanner,
    InsideHigherEdStore,
    canonicalize_job_url,
    raw_job_to_summary_input,
)
from src.job_summarizer import JobSummarizer


def job(job_id, posted_date, title="Assistant Professor of Materials Science"):
    return {
        "id": job_id,
        "title": title,
        "institution": "Example University",
        "location": "Example City, Massachusetts, United States",
        "postedDate": posted_date,
        "description": "Research and teach materials characterization.",
    }


class FakeClient:
    def __init__(self, jobs):
        self.jobs = jobs
        self.calls = []

    def discover_jobs(self):
        return {job_id: f"https://careers.insidehighered.com/job/{job_id}/slug/" for job_id in self.jobs}

    def fetch_job(self, job_id, url):
        self.calls.append((job_id, url))
        return FetchResult("job", 200, self.jobs[job_id])


class TestInsideHigherEd(unittest.TestCase):
    def test_scan_uses_sitemap_ids_and_june_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InsideHigherEdStore(Path(directory) / "pending.json", Path(directory) / "state.json")
            client = FakeClient({
                3514000: job(3514000, "2026-05-31"),
                3514001: job(3514001, "2026-06-01"),
                3514002: job(3514002, "2026-08-14", "Visiting Assistant Professor"),
            })
            report = InsideHigherEdScanner(client, store, workers=2, checkpoint_every=2).scan(
                bootstrap=True
            )

            self.assertEqual(report["dateExcluded"], 1)
            self.assertEqual(report["titleExcluded"], 1)
            self.assertEqual(report["queued"], 1)
            self.assertEqual([item["id"] for item in store.pending_jobs], [3514001])
            self.assertEqual(store.seen_ids, {3514000, 3514001, 3514002})
            self.assertTrue(
                all(url == f"https://careers.insidehighered.com/job/{job_id}" for job_id, url in client.calls)
            )

    def test_sitemap_404_is_remembered_without_blocking_bootstrap(self):
        class MissingClient:
            def discover_jobs(self):
                return {3539787: "https://careers.insidehighered.com/job/3539787/stale/"}

            def fetch_job(self, job_id, url):
                return FetchResult("not_found", 404)

        with tempfile.TemporaryDirectory() as directory:
            store = InsideHigherEdStore(Path(directory) / "pending.json", Path(directory) / "state.json")
            report = InsideHigherEdScanner(MissingClient(), store, workers=1).scan(bootstrap=True)

            self.assertEqual(report["missing"], 1)
            self.assertEqual(report["errors"], 0)
            self.assertEqual(store.seen_ids, {3539787})
            self.assertEqual(store.state["pendingErrors"], {})
            self.assertTrue(store.state["bootstrapComplete"])

    def test_summary_input_reconstructs_inside_higher_ed_url(self):
        normalized = raw_job_to_summary_input(job(3525969, "2026-07-08"))
        self.assertEqual(normalized["job_url"], "https://careers.insidehighered.com/job/3525969")

    def test_summary_canonicalizes_llm_url(self):
        source_job = raw_job_to_summary_input(job(3539180, "2026-08-01"))
        llm_item = {
            "title": source_job["title"],
            "url": "https://jobs.insidehighered.com/job/3539180/changed-by-model/",
            "date": "2026-08-01",
            "location": "Example University at Example City, MA",
            "description": "Researches materials characterization.",
            "fitScore": 3,
            "keywords": ["materials characterization"],
        }

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "summary.json"
            summarizer = JobSummarizer(
                "dummy",
                "test-model",
                url_canonicalizer=canonicalize_job_url,
            )
            with mock.patch.object(
                summarizer,
                "_generate_batch_summary",
                return_value=([llm_item], True),
            ):
                self.assertTrue(summarizer.summarize_jobs([source_job], str(output)))

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["items"]), 1)
            self.assertEqual(
                payload["items"][0]["url"],
                "https://careers.insidehighered.com/job/3539180",
            )
            self.assertEqual(payload["items"][0]["id"], "careers-3539180")

            payload["items"][0]["url"] = "https://jobs.insidehighered.com/job/3539180"
            payload["items"][0]["id"] = "jobs-3539180"
            output.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(summarizer, "_generate_batch_summary") as generate:
                self.assertTrue(summarizer.summarize_jobs([source_job], str(output)))
                generate.assert_not_called()

            repaired = json.loads(output.read_text(encoding="utf-8"))["items"][0]
            self.assertEqual(repaired["url"], "https://careers.insidehighered.com/job/3539180")
            self.assertEqual(repaired["id"], "careers-3539180")

    def test_canonical_url_leaves_unrelated_hosts_unchanged(self):
        url = "https://example.com/job/3539180"
        self.assertEqual(canonicalize_job_url(url), url)


if __name__ == "__main__":
    unittest.main()
