"""Workflow ordering tests."""

import unittest
from pathlib import Path


class TestPagesWorkflow(unittest.TestCase):
    def test_every_scrape_precedes_every_summary(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/pages.yml").read_text(
            encoding="utf-8"
        )
        scrape_names = [
            "Scrape arXiv papers",
            "Scrape non-Google JobSpy providers",
            "Scrape Google Jobs",
            "Discover new Interfolio jobs",
            "Discover new Chronicle jobs",
            "Discover new Inside Higher Ed jobs",
        ]
        summary_names = [
            "Summarize arXiv papers",
            "Summarize JobSpy jobs",
            "Summarize new Interfolio jobs",
            "Summarize new Chronicle jobs",
            "Summarize new Inside Higher Ed jobs",
        ]

        last_scrape = max(workflow.index(name) for name in scrape_names)
        first_summary = min(workflow.index(name) for name in summary_names)
        self.assertLess(last_scrape, first_summary)
        self.assertIn("jobsummary scan --source other", workflow)
        self.assertIn("jobsummary scan --source google", workflow)
        self.assertIn("jobsummary summarize", workflow)

    def test_both_repository_pushes_use_bounded_retries(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/pages.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("bash scripts/push_with_retry.sh origin main", workflow)
        self.assertIn("bash ../scripts/push_with_retry.sh origin main", workflow)
        self.assertNotIn("          git push origin main\n", workflow)

    def test_recovery_precedes_persistence_and_incomplete_status_follows_publication(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/pages.yml").read_text(encoding="utf-8")
        self.assertLess(workflow.index("Retry unfinished queues"), workflow.index("Commit database files"))
        self.assertLess(workflow.index("Commit and push to personal-site"), workflow.index("Report job processing totals"))
        self.assertIn("python -m src.summary_report --require-complete", workflow)

    def test_schedule_and_primary_model(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/pages.yml").read_text(encoding="utf-8")
        self.assertIn("cron: '17 4 * * *'", workflow)
        self.assertIn("timezone: 'America/New_York'", workflow)
        self.assertIn("LLM_MODEL: gemini-3.8-flash", workflow)
        self.assertIn("LLM_FALLBACK_MODELS: gemini-3.6-flash,gemini-3.5-flash-lite", workflow)


if __name__ == "__main__":
    unittest.main()
