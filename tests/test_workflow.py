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


if __name__ == "__main__":
    unittest.main()
