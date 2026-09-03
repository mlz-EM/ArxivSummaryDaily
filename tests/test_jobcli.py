"""Tests for the JobSpy command-line boundary."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from src import jobcli


class TestJobCli(unittest.TestCase):
    def test_provider_failure_exits_successfully_without_touching_feed(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            feed = output_dir / "jobsDaily.json"
            original = '{"header": {"generatedAt": "existing"}, "items": []}\n'
            feed.write_text(original, encoding="utf-8")

            with mock.patch.object(
                jobcli,
                "scrape_jobs",
                side_effect=RuntimeError("provider returned HTTP 429"),
            ), mock.patch.object(jobcli, "JobSummarizer") as summarizer_cls, mock.patch(
                "builtins.print"
            ) as print_mock:
                result = jobcli.main(
                    ["--output-dir", directory, "scan", "--source", "google"]
                )

            self.assertEqual(result, 0)
            self.assertEqual(feed.read_text(encoding="utf-8"), original)
            summarizer_cls.assert_not_called()
            self.assertTrue(
                any(
                    "JobSpy google scan failed" in str(call)
                    and "HTTP 429" in str(call)
                    for call in print_mock.call_args_list
                )
            )

    def test_google_and_other_scans_merge_into_one_pending_queue(self):
        other_jobs = pd.DataFrame(
            [
                {
                    "id": "li-1",
                    "site": "linkedin",
                    "date_posted": "2026-09-02",
                    "job_url": "https://example.com/linkedin-1",
                }
            ]
        )
        google_jobs = pd.DataFrame(
            [
                {
                    "id": "go-1",
                    "site": "google",
                    "date_posted": "2026-09-03",
                    "job_url": "https://example.com/google-1",
                }
            ]
        )
        config = {
            "site_name": ["indeed", "linkedin", "google"],
            "search_term": "professor",
            "google_search_term": "professor jobs",
        }

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            jobcli, "JOB_CONFIG", config
        ), mock.patch.object(
            jobcli, "scrape_jobs", side_effect=[other_jobs, google_jobs]
        ) as scrape_mock:
            self.assertEqual(
                jobcli.main(["--output-dir", directory, "scan", "--source", "other"]),
                0,
            )
            self.assertEqual(
                jobcli.main(["--output-dir", directory, "scan", "--source", "google"]),
                0,
            )

            pending_path = Path(directory) / jobcli.PENDING_FILENAME
            payload = json.loads(pending_path.read_text(encoding="utf-8"))

        self.assertEqual(len(payload["jobs"]), 2)
        self.assertEqual(
            {job["site"] for job in payload["jobs"]}, {"google", "linkedin"}
        )
        other_config = scrape_mock.call_args_list[0].kwargs
        google_config = scrape_mock.call_args_list[1].kwargs
        self.assertEqual(other_config["site_name"], ["indeed", "linkedin"])
        self.assertNotIn("google_search_term", other_config)
        self.assertEqual(google_config["site_name"], ["google"])

    def test_summary_clears_shared_queue_only_after_success(self):
        jobs = [
            {"job_url": "https://example.com/linkedin-1"},
            {"job_url": "https://example.com/google-1"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            pending_path = Path(directory) / jobcli.PENDING_FILENAME
            jobcli._write_pending_jobs(pending_path, jobs)
            summarizer = mock.Mock()
            summarizer.summarize_jobs.return_value = True

            with mock.patch.object(jobcli, "JobSummarizer", return_value=summarizer):
                result = jobcli.main(["--output-dir", directory, "summarize"])

            self.assertEqual(result, 0)
            summarizer.summarize_jobs.assert_called_once_with(
                jobs, str(Path(directory) / "jobsDaily.json")
            )
            self.assertFalse(pending_path.exists())

    def test_incomplete_summary_retains_shared_queue(self):
        jobs = [{"job_url": "https://example.com/retry"}]
        with tempfile.TemporaryDirectory() as directory:
            pending_path = Path(directory) / jobcli.PENDING_FILENAME
            jobcli._write_pending_jobs(pending_path, jobs)
            summarizer = mock.Mock()
            summarizer.summarize_jobs.return_value = False

            with mock.patch.object(jobcli, "JobSummarizer", return_value=summarizer):
                result = jobcli.main(["--output-dir", directory, "summarize"])

            self.assertEqual(result, 1)
            self.assertTrue(pending_path.exists())


if __name__ == "__main__":
    unittest.main()
