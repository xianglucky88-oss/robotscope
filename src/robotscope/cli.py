"""CLI: legacy record pipeline and the crawl planner.

Two entry points, kept deliberately separate:

``python -m robotscope.cli``
    Runs the original offline record pipeline (unchanged behaviour: prints
    ``{fed, rows}``).

``python -m robotscope.cli --plan --input fixtures/crawl.json [--offline fixtures/offline_routes.json]``
    Runs the crawl planner against a structured input file and prints the plan
    report as JSON.  ``--offline`` replays sitemap/robots/page responses from a
    JSON file so the flow works without network access.  ``--format summary``
    prints a short human-readable digest.

``--run --offline ...`` additionally dispatches and fetches pages, then prints
the execution result and the refreshed report.

Without ``--input`` the legacy fixture is used and the planner reports the
legacy records as non-crawlable items (it never invents a URL or an admission
decision).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import pipeline
from .compat import parse_input
from .fetch import OfflineFetcher
from .loader import load as load_fixture
from .planner import Planner
from .state import State


def _load_input(path):
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return load_fixture()


def _build_planner(state, request):
    return Planner(
        state,
        crawl_id=request.crawl_id,
        user_agent=request.user_agent,
        rules=request.rules,
        limits=request.limits,
        min_interval=request.min_interval,
        global_slots=request.global_slots,
        host_slots=request.host_slots,
        lease_ttl=request.lease_ttl,
    )


def _seed(planner, request, fetcher, *, now=None):
    if request.sitemaps:
        planner.discover(request.sitemaps, fetcher=fetcher, now=now)
    for url in request.urls:
        planner.add_url(url, now=now, priority=request.priority_for(url),
                        deadline=request.deadline_for(url),
                        historical_score=request.history_for(url),
                        cost=request.cost_for(url))
    planner.assess(robots_fetcher=fetcher, now=now,
                   refresh_robots=fetcher is not None)


def _run_legacy(args) -> dict:
    result = pipeline.run()
    if args.input:
        result["input"] = str(args.input)
    return result


def _run_planner(args) -> dict:
    raw = _load_input(args.input)
    request = parse_input(raw)
    state = State(args.db)
    try:
        planner = _build_planner(state, request)
        fetcher = OfflineFetcher.from_file(args.offline) if args.offline else None
        _seed(planner, request, fetcher)
        if args.run:
            if fetcher is None:
                raise SystemExit("--run requires --offline (no network in this build)")
            execution = planner.run_batch(fetcher)
            result = {
                "executed": execution["executed"],
                "failed": execution["failed"],
                "deferred": execution["deferred"],
                "counts": execution["counts"],
                "report": planner.report(),
            }
        else:
            result = planner.report()
        result["input"] = {
            "crawl_id": request.crawl_id,
            "sitemaps": request.sitemaps,
            "urls": len(request.urls),
            "legacy_records": len(request.legacy),
        }
        return result
    finally:
        state.close()


def summary_lines(report: dict) -> list[str]:
    """Short human-readable digest of a planner report."""
    counts = report.get("counts", {})
    budget = report.get("budget", {})
    coverage = report.get("coverage", {})
    lines = [
        f"crawl      {report.get('crawl_id')}  day={budget.get('day')}",
        f"discovered {counts.get('known', coverage.get('discovered', 0))}"
        f"  (sitemap complete={coverage.get('sitemap_complete')},"
        f" deletion={coverage.get('deletion_inference')})",
        f"admitted   ready={counts.get('ready')} denied={counts.get('denied')}",
        f"waiting    host={counts.get('waiting_host')} budget={counts.get('waiting_budget')}"
        f" slot={counts.get('waiting_slot')} evidence={counts.get('unassessed')}",
        f"quota      used={budget.get('used')} limit={budget.get('limit')}"
        f" remaining={budget.get('remaining')} reset={budget.get('next_reset_day')}",
    ]
    denied = report.get("coverage", {}).get("denial_reasons") or {}
    if denied:
        lines.append("denials    " + ", ".join(f"{k}={v}" for k, v in sorted(denied.items())))
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="robotscope")
    parser.add_argument("--input", help="structured crawl input (JSON)")
    parser.add_argument("--db", default=None, help="SQLite path for planner state")
    parser.add_argument("--offline", help="offline URL response map (JSON)")
    parser.add_argument("--plan", action="store_true",
                        help="run the crawl planner instead of the legacy record pipeline")
    parser.add_argument("--plan-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run", action="store_true",
                        help="also dispatch and execute the planned batch (needs --offline)")
    parser.add_argument("--format", choices=("json", "summary"), default="json")
    args = parser.parse_args(argv)
    use_planner = args.plan or args.plan_only or args.run or args.input
    if use_planner:
        from .config import ROOT
        if args.db is None:
            args.db = str(ROOT / "robotscope.db")
        result = _run_planner(args)
    else:
        result = _run_legacy(args)
    if args.format == "summary" and use_planner:
        report = result.get("report", result)
        print("\n".join(summary_lines(report)))
    else:
        print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
