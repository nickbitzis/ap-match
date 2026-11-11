"""Command line: simulate a year and gate it in CI, score capture, render sample PDFs, or serve the API and UI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .pipeline import Config, gate, run


def _money(x: float) -> str:
    return f"${x:,.0f}"


def markdown(rep: dict) -> str:
    k, c, d = rep["kpis"], rep["controls"], rep["kpis"]["discount"]
    out = [
        "## AP Match results",
        "",
        f"{rep['counts']['invoices']:,} invoices generated, {k['invoices']:,} received by {rep['as_of']} "
        f"(seed {rep['seed']}).",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Touchless rate | {k['touchless_rate']:.1%} |",
        f"| Exception rate | {k['exception_rate']:.1%} |",
        f"| Cycle time, median / p90 (hours) | {k['cycle_hours']['median']} / {k['cycle_hours']['p90']} |",
        f"| Cycle time with exceptions, median (hours) | {k['cycle_hours']['with_exceptions_median']} |",
        f"| Cost per invoice (modeled) | ${k['cost_per_invoice']:.2f} |",
        f"| Discounts captured / worth taking | {_money(d['discount_captured'])} / {_money(d['discount_available'])} "
        f"({(d['capture_rate'] or 0):.1%}) |",
        f"| Discounts declined (below cost of cash) | {_money(d['discount_declined'])} |",
        f"| Days payable outstanding | {k['dpo_days']} |",
        f"| Planted duplicates approved | {c['duplicates_approved']} of {c['planted_duplicates']} |",
        f"| Planted bank fraud approved | {c['bank_fraud_approved']} of {c['planted_bank_fraud']} |",
        f"| Segregation of duties violations | {c['sod_violations']} |",
    ]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="apmatch")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("run", help="simulate a year of AP and write report.json")
    a.add_argument("--pos", type=int, default=25_000)
    a.add_argument("--suppliers", type=int, default=400)
    a.add_argument("--seed", type=int, default=42)
    a.add_argument("--out", type=Path, default=Path("data/report.json"))
    a.add_argument("--summary", type=Path, help="append a markdown summary here (CI step summary)")
    a.add_argument("--gate", action="store_true", help="exit 1 if a control fails or touchless drops below the floor")
    a.add_argument("--min-touchless", type=float, default=0.78)
    e = sub.add_parser("eval-capture", help="field accuracy per layout for an extractor")
    e.add_argument("--extractor", choices=["offline", "claude"], default="offline")
    e.add_argument("--limit", type=int, default=200, help="invoices to score (each is one API call for claude)")
    e.add_argument("--pos", type=int, default=2000)
    p = sub.add_parser("pdfs", help="render a few invoices as simple PDFs (needs the pdf extra)")
    p.add_argument("--count", type=int, default=5)
    p.add_argument("--out", type=Path, default=Path("data/pdfs"))
    s = sub.add_parser("serve", help="run the API and web UI")
    s.add_argument("--host", default=None)
    s.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)

    if args.cmd == "serve":  # pragma: no cover
        from .app import serve

        serve(args.host, args.port)
        return
    if args.cmd == "pdfs":  # pragma: no cover - optional dependency
        from .generator import Config as GenConfig
        from .generator import generate
        from .pdf import write_pdfs

        world = generate(GenConfig(suppliers=40, pos=300))
        for path in write_pdfs(
            [i for i in world.invoices if i.channel == "pdf"] or world.invoices, args.out, args.count
        ):
            print(f"wrote {path}")
        return
    if args.cmd == "eval-capture":
        from .capture import ClaudeExtractor, OfflineExtractor, evaluate
        from .generator import Config as GenConfig
        from .generator import generate

        world = generate(GenConfig(suppliers=80, pos=args.pos))
        ex = ClaudeExtractor() if args.extractor == "claude" else OfflineExtractor()
        print(json.dumps(evaluate(world.invoices, world.suppliers, ex, args.limit), indent=2))
        return

    res = run(Config(suppliers=args.suppliers, pos=args.pos, seed=args.seed))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res.report, indent=2, default=str))
    md = markdown(res.report)
    print(md)
    if args.summary:
        with open(args.summary, "a") as f:
            f.write(md)
    if args.gate:
        fails = gate(res.report, args.min_touchless)
        for f in fails:
            print(f"GATE FAIL: {f}", file=sys.stderr)
        if fails:
            sys.exit(1)
        print("gate passed")


if __name__ == "__main__":  # pragma: no cover
    main()
