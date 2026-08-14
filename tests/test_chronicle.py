"""Tests for Chronicle sitemap discovery and compact queue persistence."""

import json
import tempfile
import unittest
from pathlib import Path

from src.chronicle_client import (
    ChronicleScanner,
    ChronicleStore,
    FetchResult,
    is_title_candidate,
    parse_job_html,
    parse_sitemap,
    raw_job_to_summary_input,
)
from src.madgex_client import MAX_RETRY_DELAY, MadgexClient


def job(job_id, title="Assistant Professor of Materials Science"):
    return {
        "id": job_id,
        "title": title,
        "institution": "Example University",
        "location": "Example City, Massachusetts, United States",
        "postedDate": "2026-08-14",
        "description": "Research and teach materials characterization.",
    }


class FakeClient:
    def __init__(self, jobs):
        self.jobs = jobs
        self.calls = []

    def discover_jobs(self):
        return {job_id: f"https://jobs.chronicle.com/job/{job_id}/slug/" for job_id in self.jobs}

    def fetch_job(self, job_id, url):
        self.calls.append((job_id, url))
        return FetchResult("job", 200, self.jobs[job_id])


class TestChronicleParsing(unittest.TestCase):
    def test_sitemap_and_job_html(self):
        sitemap = """ï»¿<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>https://jobs.chronicle.com/job/38000001/example/</loc></url>
        <url><loc>https://jobs.chronicle.com/employer/1/example/</loc></url></urlset>"""
        self.assertEqual(
            parse_sitemap(sitemap),
            {38000001: "https://jobs.chronicle.com/job/38000001/example/"},
        )

        data = [{
            "JobId": "38000001",
            "job": "Assistant Professor of Materials Science",
            "recruiter": "Example University",
            "LocationDescription": "Boston, Massachusetts, United States",
            "JobDatePosted": "Aug 14, 2026",
        }]
        page = f"""
        <script>var ClientGoogleTagManagerDataLayer = {json.dumps(data)}</script>
        <section id="job-description"><div class="mds-prose"><b>Job Description</b><br>
        Study <strong>advanced materials</strong>.</div></section>
        <section id="company-information"></section>
        """
        parsed = parse_job_html(page, 38000001, observed_at="2026-08-14T12:00:00Z")
        self.assertEqual(parsed["id"], 38000001)
        self.assertEqual(parsed["postedDate"], "2026-08-14")
        self.assertIn("Study advanced materials.", parsed["description"])
        self.assertNotIn("url", parsed)
        self.assertNotIn("salary", parsed)

        legacy_page = f"""
        <script>var ClientGoogleTagManagerDataLayer = {json.dumps(data)}</script>
        <div class="mds-surface__inner"><h2 class="mds-visually-hidden">Job Details</h2>
        <div class="mds-prose">Legacy <strong>description</strong>.</div></div>
        <div class="mds-surface__inner mds-border-top"></div>
        """
        legacy = parse_job_html(legacy_page, 38000001)
        self.assertEqual(legacy["description"], "Legacy description.")
        self.assertEqual(
            set(legacy),
            {"id", "title", "institution", "location", "postedDate", "description"},
        )

    def test_title_filter_requires_faculty_like_term_after_exclusions(self):
        self.assertTrue(is_title_candidate("Assistant Professor of Anthropology"))
        self.assertTrue(is_title_candidate("Open-Rank Position in Materials Science"))
        self.assertTrue(is_title_candidate("Tenure Track Position in Materials Science"))
        self.assertTrue(is_title_candidate("Tenure-Track Position in Materials Science"))
        self.assertFalse(is_title_candidate("Non-Tenure-Track Position in Materials Science"))
        self.assertFalse(is_title_candidate("Research Scientist in Materials Science"))
        self.assertFalse(is_title_candidate("Visiting Assistant Professor"))
        self.assertFalse(is_title_candidate("Postdoctoral Fellowship"))

    def test_server_retry_delay_is_bounded(self):
        response = type("Response", (), {"headers": {"Retry-After": "3600"}})()
        self.assertEqual(MadgexClient()._retry_seconds(response, 0), MAX_RETRY_DELAY)


class TestChronicleStoreAndScanner(unittest.TestCase):
    def test_scan_queues_candidates_and_remembers_excluded_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ChronicleStore(Path(directory) / "pending.json", Path(directory) / "state.json")
            client = FakeClient({
                38000001: job(38000001),
                38000002: job(38000002, "Adjunct Instructor of Art"),
            })
            scanner = ChronicleScanner(client, store, workers=2, checkpoint_every=1)

            report = scanner.scan(bootstrap=True)

            self.assertEqual(report["queued"], 1)
            self.assertEqual(report["titleExcluded"], 1)
            self.assertEqual({item["id"] for item in store.pending_jobs}, {38000001})
            self.assertEqual(store.seen_ids, {38000001, 38000002})
            self.assertTrue(store.state["bootstrapComplete"])

            reloaded = ChronicleStore(Path(directory) / "pending.json", Path(directory) / "state.json")
            second = ChronicleScanner(client, reloaded, workers=2).scan()
            self.assertEqual(second["requests"], 0)
            self.assertEqual(second["knownSkipped"], 2)
            self.assertTrue(all(url == f"https://jobs.chronicle.com/job/{job_id}" for job_id, url in client.calls))

    def test_processed_pending_jobs_can_be_compacted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ChronicleStore(Path(directory) / "pending.json", Path(directory) / "state.json")
            store.add_job(job(38000001))
            store.remove_pending([38000001])
            store.save()
            self.assertEqual(store.pending_jobs, [])
            self.assertIn(38000001, store.seen_ids)
            store.remove_pending_file_if_empty()
            self.assertFalse((Path(directory) / "pending.json").exists())

        normalized = raw_job_to_summary_input(job(38000001))
        self.assertEqual(normalized["job_url"], "https://jobs.chronicle.com/job/38000001")
        self.assertEqual(normalized["company"], "Example University")


if __name__ == "__main__":
    unittest.main()
