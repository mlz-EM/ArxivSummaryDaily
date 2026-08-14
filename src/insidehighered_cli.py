"""Command-line workflow for Inside Higher Ed job discovery and summaries."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from config import settings

from .feed_utils import utc_generated_at
from .insidehighered_client import (
    DEFAULT_MINIMUM_POSTED_DATE,
    DEFAULT_WORKERS,
    InsideHigherEdClient,
    InsideHigherEdScanner,
    InsideHigherEdStore,
    raw_job_to_summary_input,
)
from .job_summarizer import JobSummarizer


DEFAULT_CONFIG = {
    "minimum_posted_date": DEFAULT_MINIMUM_POSTED_DATE,
    "request_delay": 0.02,
    "timeout": 30,
    "retry_count": 4,
    "retry_delay": 2,
    "checkpoint_every": 200,
    "workers": DEFAULT_WORKERS,
    "summary_batch_size": 20,
}


def _config() -> Dict[str, Any]:
    return {**DEFAULT_CONFIG, **getattr(settings, "INSIDE_HIGHER_ED_CONFIG", {})}


def _default_path(output_dir: str, filename: str) -> str:
    return str(Path(output_dir) / filename)


def _build_parser() -> argparse.ArgumentParser:
    config = _config()
    parser = argparse.ArgumentParser(description="Inside Higher Ed job discovery and summary pipeline")
    parser.add_argument("--output-dir", default=getattr(settings, "OUTPUT_DIR", "data"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="discover new jobs from Inside Higher Ed's sitemap")
    scan.add_argument("--mode", choices=("bootstrap", "daily"), required=True)
    scan.add_argument(
        "--minimum-posted-date",
        default=str(config["minimum_posted_date"]),
        help="queue only jobs posted on or after this YYYY-MM-DD date",
    )
    scan.add_argument("--request-delay", type=float, default=float(config["request_delay"]))
    scan.add_argument("--timeout", type=float, default=float(config["timeout"]))
    scan.add_argument("--retry-count", type=int, default=int(config["retry_count"]))
    scan.add_argument("--retry-delay", type=float, default=float(config["retry_delay"]))
    scan.add_argument("--checkpoint-every", type=int, default=int(config["checkpoint_every"]))
    scan.add_argument("--workers", type=int, default=int(config["workers"]))
    scan.add_argument("--pending-file")
    scan.add_argument("--state-file")

    summarize = subparsers.add_parser("summarize", help="summarize all queued Inside Higher Ed jobs")
    summarize.add_argument("--pending-file")
    summarize.add_argument("--state-file")
    summarize.add_argument("--output-file")
    summarize.add_argument("--max-jobs", type=int, default=0, help="0 processes the entire queue")
    summarize.add_argument("--batch-size", type=int, default=int(config["summary_batch_size"]))
    return parser


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    pending = Path(
        args.pending_file or _default_path(args.output_dir, "insideHigherEdJobsPending.json")
    )
    state = Path(args.state_file or _default_path(args.output_dir, "insideHigherEdScanState.json"))
    return pending, state


def _scan(args: argparse.Namespace) -> int:
    if os.getenv("INSIDE_HIGHER_ED_ENABLE_LIVE", "").lower() not in {"1", "true", "yes"}:
        raise RuntimeError(
            "live Inside Higher Ed fetching is disabled; set INSIDE_HIGHER_ED_ENABLE_LIVE=true "
            "after confirming authorization"
        )
    pending_path, state_path = _paths(args)
    store = InsideHigherEdStore(pending_path, state_path)
    if args.mode == "daily" and not store.state.get("bootstrapComplete"):
        raise RuntimeError(
            "initial Inside Higher Ed bootstrap is incomplete; run scan --mode bootstrap first"
        )
    client = InsideHigherEdClient(
        request_delay=args.request_delay,
        timeout=args.timeout,
        retry_count=args.retry_count,
        retry_delay=args.retry_delay,
    )
    scanner = InsideHigherEdScanner(
        client,
        store,
        minimum_posted_date=args.minimum_posted_date,
        workers=args.workers,
        checkpoint_every=args.checkpoint_every,
    )
    report = scanner.scan(bootstrap=args.mode == "bootstrap")
    print(json.dumps(report, sort_keys=True))
    if args.mode == "bootstrap" and report["errors"]:
        return 1
    return 0


def _set_header(path: Path, model: str) -> None:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["header"] = {
        "title": "Basic Info",
        "model": model,
        "generatedAt": utc_generated_at(),
        "source": "Inside Higher Ed Faculty Jobs",
        "notes": [
            "This report contains AI-filtered academic positions discovered from Inside Higher Ed Careers.",
            "The initial feed includes current sitemap jobs posted on or after June 1, 2026; daily runs summarize only newly seen listings.",
        ],
    }
    InsideHigherEdStore._atomic_write(path, payload)


def _summarize(args: argparse.Namespace) -> int:
    pending_path, state_path = _paths(args)
    store = InsideHigherEdStore(pending_path, state_path)
    pending: List[Dict[str, Any]] = sorted(store.pending_jobs, key=lambda job: int(job["id"]))
    if args.max_jobs > 0:
        pending = pending[: args.max_jobs]

    output_path = Path(
        args.output_file or _default_path(args.output_dir, "insideHigherEdJobsSummary.json")
    )
    llm_config = settings.LLM_CONFIG
    api_key = str(llm_config.get("api_key") or "").strip()
    if pending and (not api_key or api_key == "YOUR_API_HERE"):
        raise RuntimeError("LLM_API_KEY is required to summarize pending Inside Higher Ed jobs")

    summarizer = JobSummarizer(api_key, llm_config.get("model"))
    summarizer.max_papers_per_batch = max(1, int(args.batch_size))
    normalized = [raw_job_to_summary_input(job) for job in pending]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    success = summarizer.summarize_jobs(normalized, str(output_path))
    if output_path.exists():
        _set_header(output_path, summarizer.client.model)
    if not success:
        print("Inside Higher Ed summary was incomplete; queued records were retained for retry.")
        return 1

    store.remove_pending(int(job["id"]) for job in pending)
    store.save()
    store.remove_pending_file_if_empty()
    print(f"Summarized {len(pending)} Inside Higher Ed jobs and removed them from the pending queue.")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            return _scan(args)
        if args.command == "summarize":
            return _summarize(args)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
