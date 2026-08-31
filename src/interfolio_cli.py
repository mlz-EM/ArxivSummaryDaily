"""Command-line workflow for Interfolio discovery and summarization."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from config import settings

from .feed_utils import eastern_generated_at
from .interfolio_client import (
    DEFAULT_LOOKAHEAD,
    DEFAULT_RECENT_DAYS,
    DEFAULT_START_ID,
    InterfolioClient,
    InterfolioScanner,
    InterfolioStore,
    save_tenant_directory,
    is_summary_candidate,
    load_processed_ids,
    raw_job_to_summary_input,
    save_processed_ids,
)
from .job_summarizer import JobSummarizer


DEFAULT_CONFIG = {
    "start_id": DEFAULT_START_ID,
    "lookahead": DEFAULT_LOOKAHEAD,
    "recent_days": DEFAULT_RECENT_DAYS,
    "request_delay": 0.5,
    "timeout": 30,
    "retry_count": 4,
    "retry_delay": 2,
    "checkpoint_every": 50,
    "workers": 8,
    "summary_batch_size": 50,
    "minimum_posted_date": "2026-06-01",
}


def _config() -> Dict[str, Any]:
    return {**DEFAULT_CONFIG, **getattr(settings, "INTERFOLIO_CONFIG", {})}


def _default_path(output_dir: str, filename: str) -> str:
    return str(Path(output_dir) / filename)


def _build_parser() -> argparse.ArgumentParser:
    config = _config()
    parser = argparse.ArgumentParser(description="Interfolio job discovery and summary pipeline")
    parser.add_argument("--output-dir", default=getattr(settings, "OUTPUT_DIR", "data"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="discover new raw Interfolio positions")
    scan.add_argument("--mode", choices=("bootstrap", "daily"), required=True)
    scan.add_argument("--start-id", type=int, default=int(config["start_id"]))
    scan.add_argument("--lookahead", type=int, default=int(config["lookahead"]))
    scan.add_argument("--recent-days", type=int, default=int(config["recent_days"]))
    scan.add_argument("--request-delay", type=float, default=float(config["request_delay"]))
    scan.add_argument("--timeout", type=float, default=float(config["timeout"]))
    scan.add_argument("--retry-count", type=int, default=int(config["retry_count"]))
    scan.add_argument("--retry-delay", type=float, default=float(config["retry_delay"]))
    scan.add_argument("--checkpoint-every", type=int, default=int(config["checkpoint_every"]))
    scan.add_argument("--workers", type=int, default=int(config["workers"]))
    scan.add_argument("--pending-file")
    scan.add_argument("--state-file")

    summarize = subparsers.add_parser("summarize", help="summarize queued jobs not processed before")
    summarize.add_argument("--pending-file")
    summarize.add_argument("--state-file")
    summarize.add_argument("--output-file")
    summarize.add_argument("--max-jobs", type=int, default=0, help="0 processes all pending jobs")
    summarize.add_argument("--batch-size", type=int, default=int(config["summary_batch_size"]))
    summarize.add_argument(
        "--minimum-posted-date",
        default=str(config["minimum_posted_date"]),
        help="send only jobs posted on or after this YYYY-MM-DD date",
    )

    directory = subparsers.add_parser(
        "directory", help="write the unique Interfolio tenant-ID and school-name directory"
    )
    directory.add_argument("--pending-file")
    directory.add_argument("--output-file")

    return parser


def _scan(args: argparse.Namespace) -> int:
    if os.getenv("INTERFOLIO_ENABLE_LIVE", "").lower() not in {"1", "true", "yes"}:
        raise RuntimeError(
            "live Interfolio fetching is disabled; set INTERFOLIO_ENABLE_LIVE=true after confirming authorization"
        )
    pending_path = Path(
        args.pending_file or _default_path(args.output_dir, "interfolioJobsPending.json")
    )
    state_path = Path(args.state_file or _default_path(args.output_dir, "interfolioScanState.json"))
    store = InterfolioStore(pending_path, state_path)
    client = InterfolioClient(
        request_delay=args.request_delay,
        timeout=args.timeout,
        retry_count=args.retry_count,
        retry_delay=args.retry_delay,
    )
    scanner = InterfolioScanner(
        client,
        store,
        start_id=args.start_id,
        lookahead=args.lookahead,
        recent_days=args.recent_days,
        checkpoint_every=args.checkpoint_every,
        workers=args.workers,
    )
    report = scanner.bootstrap() if args.mode == "bootstrap" else scanner.daily()
    print(json.dumps(report, sort_keys=True))
    if report["errors"]:
        print("Interfolio discovery completed with errors; failed IDs remain queued for retry.")
    if args.mode == "bootstrap" and not store.state.get("bootstrapComplete"):
        return 1
    return 0


def _load_pending_jobs(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    if not isinstance(jobs, list):
        raise ValueError(f"pending Interfolio jobs must be an array: {path}")
    return [job for job in jobs if isinstance(job, dict) and str(job.get("id", "")).isdigit()]


def _directory(args: argparse.Namespace) -> int:
    pending_path = Path(
        args.pending_file or _default_path(args.output_dir, "interfolioJobsPending.json")
    )
    output_path = Path(
        args.output_file or _default_path(args.output_dir, "interfolioTenantDirectory.json")
    )
    changed = save_tenant_directory(output_path, _load_pending_jobs(pending_path))
    state = "Updated" if changed else "Unchanged"
    print(f"{state} Interfolio tenant directory: {output_path}")
    return 0


def _set_interfolio_header(path: Path, model: str) -> None:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["header"] = {
        "title": "Basic Info",
        "model": model,
        "generatedAt": eastern_generated_at(),
        "source": "Interfolio Faculty Jobs",
        "notes": [
            "This report contains AI-filtered academic positions discovered from public Interfolio job pages.",
            "Only the compact scan state and AI summary are retained after queued records are processed.",
        ],
    }
    InterfolioStore._atomic_write(path, payload)


def _summarize(args: argparse.Namespace) -> int:
    pending_path = Path(
        args.pending_file or _default_path(args.output_dir, "interfolioJobsPending.json")
    )
    state_path = Path(args.state_file or _default_path(args.output_dir, "interfolioSummaryState.json"))
    output_path = Path(args.output_file or _default_path(args.output_dir, "interfolioJobsSummary.json"))

    pending_jobs = _load_pending_jobs(pending_path)
    processed_ids = load_processed_ids(state_path)
    unprocessed = [job for job in pending_jobs if int(job["id"]) not in processed_ids]
    before_cutoff = [
        job
        for job in unprocessed
        if not str(job.get("postedDate") or "")
        or str(job.get("postedDate") or "") < args.minimum_posted_date
    ]
    if before_cutoff:
        processed_ids.update(int(job["id"]) for job in before_cutoff)
        save_processed_ids(state_path, processed_ids)
        print(
            f"Marked {len(before_cutoff)} Interfolio records before "
            f"{args.minimum_posted_date} as processed without sending them to the LLM."
        )
    pending = [
        job
        for job in unprocessed
        if str(job.get("postedDate") or "") >= args.minimum_posted_date
    ]
    pending.sort(key=lambda job: int(job["id"]))
    if args.max_jobs > 0:
        pending = pending[: args.max_jobs]

    candidates = [job for job in pending if is_summary_candidate(job)]
    normalized_jobs = [raw_job_to_summary_input(job) for job in candidates]
    llm_config = settings.LLM_CONFIG
    api_key = str(llm_config.get("api_key") or "").strip()
    if candidates and (not api_key or api_key == "YOUR_API_HERE"):
        raise RuntimeError("LLM_API_KEY is required to summarize pending Interfolio candidates")
    summarizer = JobSummarizer(api_key, llm_config.get("model"))
    summarizer.max_papers_per_batch = max(1, int(args.batch_size))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    success = summarizer.summarize_jobs(normalized_jobs, str(output_path))
    if output_path.exists():
        _set_interfolio_header(output_path, summarizer.client.model)
    if not success:
        print("Interfolio summary was incomplete; pending IDs were not marked processed.")
        return 1

    processed_ids.update(int(job["id"]) for job in pending)
    save_processed_ids(state_path, processed_ids)
    if pending_path.exists() and all(int(job["id"]) in processed_ids for job in pending_jobs):
        pending_path.unlink()
    print(
        f"Processed {len(pending)} post-cutoff Interfolio records: "
        f"sent {len(candidates)} eligible positions to the LLM and skipped "
        f"{len(pending) - len(candidates)} closed or title-excluded positions."
    )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            return _scan(args)
        if args.command == "summarize":
            return _summarize(args)
        if args.command == "directory":
            return _directory(args)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
