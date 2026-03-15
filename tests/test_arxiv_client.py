"""ArxivClient and feed contract tests."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from config.settings import CATEGORIES, QUERY, SEARCH_CONFIG
from src import cli
from src.arxiv_client import ArxivClient
from src.job_summarizer import JobSummarizer
from src.paper_summarizer import PaperSummarizer


class TestArxivClient(unittest.TestCase):
    def setUp(self):
        self.client = ArxivClient()
        self.test_output_dir = "test_output"
        self.test_filename = "test_metadata.json"

    def test_search_papers_with_settings(self):
        results = self.client.search_papers(categories=CATEGORIES, query=QUERY)

        self.assertIsInstance(results, list)
        self.assertLessEqual(len(results), SEARCH_CONFIG["max_total_results"])

        if results:
            paper = results[0]
            required_fields = ["title", "authors", "published", "summary", "categories"]
            for field in required_fields:
                self.assertIn(field, paper)

            self.assertTrue(
                any(cat in paper["categories"] for cat in CATEGORIES),
                f"Paper categories {paper['categories']} not in expected {CATEGORIES}",
            )

    def test_save_results(self):
        results = [{"title": "Test Paper", "authors": ["Test Author"], "entry_id": "id-1", "published": "2026-03-10T00:00:00"}]

        self.client.save_results(results, self.test_output_dir, self.test_filename)

        output_path = Path(self.test_output_dir) / self.test_filename
        self.assertTrue(output_path.exists())

        with open(output_path, "r", encoding="utf-8") as f:
            saved_data = json.load(f)

        self.assertEqual(saved_data, results)

        output_path.unlink()
        Path(self.test_output_dir).rmdir()


class TestIncrementalPaperFlow(unittest.TestCase):
    def test_cli_refreshes_feeds_even_without_new_fetches(self):
        with tempfile.TemporaryDirectory() as output_dir:
            args = SimpleNamespace(
                query="electron microscopy",
                categories=["physics.optics"],
                max_results=5,
                output_dir=output_dir,
            )

            with mock.patch("argparse.ArgumentParser.parse_args", return_value=args), mock.patch.object(
                cli, "LAST_RUN_FILE", "last_run.json"
            ), mock.patch.object(cli, "ArxivClient") as mock_client_cls, mock.patch.object(
                cli, "PaperSummarizer"
            ) as mock_summarizer_cls:
                mock_client = mock_client_cls.return_value
                mock_client.search_papers.return_value = []

                mock_summarizer = mock_summarizer_cls.return_value
                mock_summarizer.summarize_papers.return_value = True

                cli.main()

                mock_summarizer.summarize_papers.assert_called_once_with(
                    [],
                    os.path.join(output_dir, "arXivDaily.json"),
                )
                mock_client.save_last_run_info.assert_not_called()

    def test_cli_saves_last_run_after_successful_incremental_fetch(self):
        with tempfile.TemporaryDirectory() as output_dir:
            args = SimpleNamespace(
                query="electron microscopy",
                categories=["physics.optics"],
                max_results=5,
                output_dir=output_dir,
            )
            papers = [
                {
                    "entry_id": "http://arxiv.org/abs/2603.99999v1",
                    "title": "Fresh Paper",
                }
            ]

            with mock.patch("argparse.ArgumentParser.parse_args", return_value=args), mock.patch.object(
                cli, "LAST_RUN_FILE", "last_run.json"
            ), mock.patch.object(cli, "ArxivClient") as mock_client_cls, mock.patch.object(
                cli, "PaperSummarizer"
            ) as mock_summarizer_cls:
                mock_client = mock_client_cls.return_value
                mock_client.search_papers.return_value = papers

                mock_summarizer = mock_summarizer_cls.return_value
                mock_summarizer.summarize_papers.return_value = True

                cli.main()

                mock_client.save_last_run_info.assert_called_once_with(
                    "http://arxiv.org/abs/2603.99999v1",
                    os.path.join(output_dir, "last_run.json"),
                    1,
                )

    def test_paper_summarizer_writes_json_contract(self):
        with tempfile.TemporaryDirectory() as output_dir:
            output_json = os.path.join(output_dir, "arXivDaily.json")
            existing = {
                "header": {
                    "title": "Basic Info",
                    "model": "test-model",
                    "generatedAt": "2026-03-14T23:31:44Z",
                    "source": "TTAP Daily Feed",
                    "notes": [],
                },
                "items": [
                    {
                        "id": "arxiv-2603.11643v1",
                        "title": "Sample",
                        "url": "https://arxiv.org/abs/2603.11643v1",
                        "date": "2026-03-14",
                        "summary": "Short abstract summary text",
                        "tags": ["materials-science"],
                    }
                ],
            }
            with open(output_json, "w", encoding="utf-8") as f:
                json.dump(existing, f)

            summarizer = PaperSummarizer(api_key="dummy", model="test-model")
            with mock.patch.object(summarizer, "_filter_new_papers", return_value=[]):
                success = summarizer.summarize_papers([], output_json)

            self.assertTrue(success)
            self.assertTrue(os.path.exists(output_json))

            with open(output_json, "r", encoding="utf-8") as f:
                payload = json.load(f)

            self.assertIn("header", payload)
            self.assertIsInstance(payload["header"], dict)
            self.assertIn("generatedAt", payload["header"])
            self.assertTrue(payload["header"]["generatedAt"].endswith("Z"))
            self.assertEqual(payload["header"].get("source"), "TTAP Daily Feed")
            self.assertIsInstance(payload.get("items"), list)
            self.assertEqual(len(payload["items"]), 1)

            item = payload["items"][0]
            self.assertEqual(sorted(item.keys()), ["date", "id", "summary", "tags", "title", "url"])
            self.assertRegex(item["date"], r"^\d{4}-\d{2}-\d{2}$")
            self.assertTrue(item["url"].startswith("https://"))
            self.assertIsInstance(item["tags"], list)


class TestIncrementalJobFlow(unittest.TestCase):
    def test_job_summarizer_skips_jobs_already_present_in_json(self):
        job = {
            "title": "Faculty Role",
            "company": "Example University",
            "location": "Boston, MA",
            "date_posted": "2026-03-10",
            "description": "Existing description.",
            "job_url": "https://example.com/job-1",
        }

        with tempfile.TemporaryDirectory() as output_dir:
            output_json = os.path.join(output_dir, "jobsDaily.json")
            existing = {
                "header": {
                    "title": "Basic Info",
                    "model": "test-model",
                    "generatedAt": "2026-03-14T23:31:44Z",
                    "source": "TTAP Daily Feed",
                    "notes": [],
                },
                "items": [
                    {
                        "id": "example-job-1",
                        "title": "Faculty Role",
                        "url": "https://example.com/job-1",
                        "date": "2026-03-10",
                        "location": "Example University at Boston, MA",
                        "description": "Existing description.",
                        "fitScore": 2,
                        "isNew": True,
                        "keywords": [],
                    }
                ],
            }
            with open(output_json, "w", encoding="utf-8") as f:
                json.dump(existing, f)

            summarizer = JobSummarizer(api_key="dummy", model="test-model")

            with mock.patch.object(summarizer, "_filter_new_jobs", return_value=[job]), mock.patch.object(
                summarizer, "_generate_batch_summary"
            ) as mock_generate, mock.patch.object(summarizer, "_mark_jobs_processed") as mock_mark:
                success = summarizer.summarize_jobs([job], output_json)

            self.assertTrue(success)
            mock_generate.assert_not_called()
            mock_mark.assert_not_called()

            with open(output_json, "r", encoding="utf-8") as f:
                payload = json.load(f)

            item = payload["items"][0]
            self.assertIs(item["isNew"], False)
            self.assertIsInstance(item["fitScore"], int)
            self.assertGreaterEqual(item["fitScore"], 1)
            self.assertLessEqual(item["fitScore"], 3)

    def test_job_feed_orders_new_bucket_before_old_bucket(self):
        summarizer = JobSummarizer(api_key="dummy", model="test-model")

        items = [
            {
                "id": "old-newer",
                "title": "Old But Newer Date",
                "url": "https://example.com/old-newer",
                "date": "2026-03-15",
                "location": "X",
                "description": "old",
                "fitScore": 1,
                "isNew": False,
                "keywords": [],
            },
            {
                "id": "old-older",
                "title": "Older Old",
                "url": "https://example.com/old-older",
                "date": "2026-03-01",
                "location": "X",
                "description": "old2",
                "fitScore": 1,
                "isNew": False,
                "keywords": [],
            },
            {
                "id": "brand-new",
                "title": "Brand New",
                "url": "https://example.com/new-middate",
                "date": "2026-03-10",
                "location": "X",
                "description": "new",
                "fitScore": 2,
                "isNew": True,
                "keywords": [],
            },
        ]

        ordered = summarizer._sort_items(items)

        self.assertEqual(ordered[0]["title"], "Brand New")
        self.assertEqual(ordered[1]["title"], "Old But Newer Date")
        self.assertEqual(ordered[2]["title"], "Older Old")

    def test_ai_filtered_job_is_persisted_with_zero_fit_score(self):
        job = {
            "title": "Filtered Out Role",
            "company": "Example University",
            "location": "Austin, TX",
            "date_posted": "2026-03-10",
            "description": "Not relevant to profile.",
            "job_url": "https://example.com/job-filtered",
        }

        with tempfile.TemporaryDirectory() as output_dir:
            output_json = os.path.join(output_dir, "jobsDaily.json")
            summarizer = JobSummarizer(api_key="dummy", model="test-model")

            with mock.patch.object(summarizer, "_filter_new_jobs", return_value=[job]), mock.patch.object(
                summarizer, "_generate_batch_summary", return_value=([], True)
            ):
                success = summarizer.summarize_jobs([job], output_json)

            self.assertTrue(success)
            with open(output_json, "r", encoding="utf-8") as f:
                payload = json.load(f)

            self.assertEqual(len(payload["items"]), 1)
            item = payload["items"][0]
            self.assertEqual(item["url"], "https://example.com/job-filtered")
            self.assertEqual(item["fitScore"], 0)
            self.assertEqual(item["description"], "")


if __name__ == "__main__":
    unittest.main()
