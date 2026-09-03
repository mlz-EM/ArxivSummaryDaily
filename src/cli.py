"""Command-line workflow for separate arXiv scanning and summarization."""

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import (
    CATEGORIES,
    LAST_RUN_FILE,
    LLM_CONFIG,
    OUTPUT_DIR,
    QUERY,
    SEARCH_CONFIG,
)
from .arxiv_client import ArxivClient
from .paper_summarizer import PaperSummarizer


PENDING_FILENAME = "arXivPending.json"
PENDING_SCHEMA_VERSION = 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="arXiv paper summary generator")
    parser.add_argument("--query", default=QUERY, help="search keywords")
    parser.add_argument("--categories", nargs="+", default=CATEGORIES, help="arXiv categories")
    parser.add_argument(
        "--max-results",
        type=int,
        default=SEARCH_CONFIG["max_total_results"],
        help="number of papers to fetch",
    )
    parser.add_argument("--output-dir", default=OUTPUT_DIR, help="output directory")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("scan", help="fetch papers into the pending queue")
    subparsers.add_parser("summarize", help="summarize the pending queue")
    return parser


def _pending_path(output_dir: str) -> Path:
    return Path(output_dir) / PENDING_FILENAME


def _last_run_path(output_dir: str) -> Optional[str]:
    return os.path.join(output_dir, LAST_RUN_FILE) if LAST_RUN_FILE else None


def _load_pending(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {"latestEntryId": None, "papers": []}

    papers = payload.get("papers") if isinstance(payload, dict) else None
    if not isinstance(papers, list):
        raise ValueError(f"arXiv pending queue must contain a papers array: {path}")
    return {
        "latestEntryId": payload.get("latestEntryId"),
        "papers": [paper for paper in papers if isinstance(paper, dict)],
    }


def _write_pending(path: Path, latest_entry_id: Optional[str], papers: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    payload = {
        "schemaVersion": PENDING_SCHEMA_VERSION,
        "latestEntryId": latest_entry_id,
        "papers": papers,
    }
    with temporary_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(temporary_path, path)


def _merge_papers(
    incoming: List[Dict[str, Any]], existing: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen = set()
    for paper in incoming + existing:
        entry_id = str(paper.get("entry_id") or "").strip()
        key = entry_id or json.dumps(paper, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        merged.append(paper)
    return merged


def scan(args) -> int:
    """Fetch arXiv papers and merge them into a durable pending queue."""
    search_config = dict(SEARCH_CONFIG)
    search_config["max_total_results"] = args.max_results
    client = ArxivClient(search_config)
    papers = client.search_papers(
        categories=args.categories,
        query=args.query,
        last_run_file=_last_run_path(args.output_dir),
    )

    path = _pending_path(args.output_dir)
    existing = _load_pending(path)
    merged = _merge_papers(papers, existing["papers"])
    latest_entry_id = papers[0].get("entry_id") if papers else existing["latestEntryId"]
    _write_pending(path, latest_entry_id, merged)

    print(f"arXiv scan fetched {len(papers)} papers; {len(merged)} total records are queued.")
    return 0


def summarize(output_dir: str) -> int:
    """Summarize queued arXiv papers and advance state only on success."""
    path = _pending_path(output_dir)
    if not path.exists():
        print("No arXiv pending queue was produced; leaving the existing feed unchanged.")
        return 0

    pending = _load_pending(path)
    papers = pending["papers"]
    output_file = os.path.join(output_dir, "arXivDaily.json")
    summarizer = PaperSummarizer(LLM_CONFIG["api_key"], LLM_CONFIG.get("model"))

    try:
        success = summarizer.summarize_papers(papers, output_file)
    except Exception as exc:
        print(f"Error generating arXiv summary: {exc}")
        success = False

    if not success:
        print("arXiv summary incomplete; pending papers and run record were retained for retry.")
        return 1

    latest_entry_id = pending["latestEntryId"]
    last_run_file = _last_run_path(output_dir)
    if latest_entry_id and last_run_file:
        ArxivClient().save_last_run_info(latest_entry_id, last_run_file, len(papers))
        print(f"Summary succeeded. Next run will start from entry ID: {latest_entry_id}")
    else:
        print("No new arXiv entries were fetched. The run record was not advanced.")

    path.unlink()
    print(f"JSON feed generated and saved to: {output_file}")
    return 0


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    command = getattr(args, "command", None)
    if command == "scan":
        return scan(args)
    if command == "summarize":
        return summarize(args.output_dir)

    # Preserve the original one-command experience using the same two phases.
    scan(args)
    return summarize(args.output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
