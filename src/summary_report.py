"""Expose deferred jobs in the Actions summary, even when a step fails."""

import argparse
import json
import os
from pathlib import Path

from .llm_client import current_run_id
from .retry_pending import pending_counts


SOURCES = (
    ("JobSpy", "jobsDaily", "jobSpyJobsPending"),
    ("Interfolio", "interfolioJobsSummary", "interfolioJobsPending"),
    ("Chronicle", "chronicleJobsSummary", "chronicleJobsPending"),
    ("Inside Higher Ed", "insideHigherEdJobsSummary", "insideHigherEdJobsPending"),
)


def build_report(directory):
    directory = Path(directory)
    lines = [
        "## Job processing", "",
        "| Source | Accepted | AI rejected | Summary prefilter | Discovery prefilter | Still queued |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    notes = []
    for source, feed, queue in SOURCES:
        pending_path = directory / (queue + ".json")
        pending = json.loads(pending_path.read_text()).get("jobs", []) if pending_path.exists() else []
        report_path = directory / (feed + "Report.json")
        report = json.loads(report_path.read_text()) if report_path.exists() else {}
        if report.get("runId") != current_run_id():
            report = {}
        counts = [report.get(key, "—") for key in ("accepted", "rejected", "prefiltered")]
        state_path = directory / (queue.replace("JobsPending", "ScanState") + ".json")
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        discovery_excluded = state.get("lastScanReport", {}).get("profileExcluded", "—")
        lines.append(f"| {source} | {counts[0]} | {counts[1]} | {counts[2]} | {discovery_excluded} | {len(pending)} |")
        if pending:
            print(f"::warning::{source}: {len(pending)} jobs remain queued; they have not all been summarized.")
        if report.get("stopReason"):
            notes.extend(["", f"**{source}:** {report['stopReason']}", ""])
        if report.get("modelsUsed"):
            notes.extend(["", f"**{source} models:** {', '.join(report['modelsUsed'])}", ""])
    counts = pending_counts(directory)
    if counts["arXiv"]:
        print(f"::warning::arXiv: {counts['arXiv']} papers remain queued.")
        notes.extend(["", f"**arXiv still queued:** {counts['arXiv']}", ""])
    retry_path = directory / "summaryRetryReport.json"
    if retry_path.exists():
        retry = json.loads(retry_path.read_text())
        if retry.get("runId") == current_run_id():
            notes.extend(["", f"**Delayed recovery passes:** {len(retry.get('rounds', []))}", ""])
            if retry.get("stopReason"):
                notes.extend([retry["stopReason"], ""])
    return "\n".join(lines + notes) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args(argv)
    report = build_report(args.output_dir)
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(report)
    if args.require_complete and any(pending_counts(args.output_dir).values()):
        print("::error::Summary processing is incomplete; unresolved records remain durably queued.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
