"""Command line entry point.

  remediate.py run  FILE_OR_FOLDER...  [-o OUT.pdf] [--no-ai] [--config remediator.toml]
  remediate.py check FILE_OR_FOLDER...
  remediate.py apply OUT_accessible.pdf OUT_accessible_review.json
  remediate.py autotag FILE.pdf [-o OUT.pdf]     # Adobe AutoTag only
  remediate.py setup [--status]                  # save Claude / Adobe API keys
  remediate.py eval ORIGINAL.pdf YOUR_FINISHED.pdf  # score the tool against your own work
  remediate.py stats                             # how often you accept/edit/reject each kind of fix
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import config as config_mod
from . import credentials
from .apply import apply_review
from .pipeline import iter_inputs, output_paths, remediate


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in ("run", "check", "apply", "autotag", "setup", "eval", "stats", "benchmark", "app", "corpus", "-h", "--help"):
        argv.insert(0, "run")  # `remediate.py file.pdf` == `remediate.py run file.pdf`

    ap = argparse.ArgumentParser(prog="remediate", description="PDF accessibility remediation assistant")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="remediate PDFs")
    r.add_argument("inputs", nargs="+", help="PDF files or folders")
    r.add_argument("-o", "--output", help="output PDF (single input only)")
    r.add_argument("--no-ai", action="store_true", help="skip Claude alt-text generation")
    r.add_argument("--lang", help="document language, e.g. en-US, es-ES")
    r.add_argument("--config", help="path to remediator.toml")
    r.add_argument("--autotag", action="store_true",
                   help="re-tag with Adobe AutoTag even if the PDF already has tags")
    c = sub.add_parser("check", help="validate only, change nothing")
    c.add_argument("inputs", nargs="+")
    c.add_argument("--config")
    a = sub.add_parser("apply", help="apply decisions from a *_review.json")
    a.add_argument("pdf")
    a.add_argument("review")
    a.add_argument("-o", "--output")
    a.add_argument("--no-learn", action="store_true",
                   help="don't record these decisions (e.g. confidential client files)")
    e = sub.add_parser("eval", help="compare the tool's output with a PDF you remediated by hand")
    e.add_argument("original", help="the PDF before remediation (or the tool's output with --compare-only)")
    e.add_argument("gold", help="your finished, hand-remediated PDF")
    e.add_argument("--no-ai", action="store_true")
    e.add_argument("--compare-only", action="store_true", help="don't run the tool; compare the two files")
    e.add_argument("--config")
    sub.add_parser("stats", help="acceptance rates from your recorded decisions + eval history")
    b = sub.add_parser("benchmark", help="score the tool on public PubMed Central articles")
    b.add_argument("action", choices=["fetch", "run"])
    b.add_argument("--dir", default="benchmark/pmc")
    b.add_argument("--n", type=int, default=20, help="fetch: number of articles")
    b.add_argument("--start", default="PMC12", help="fetch: PMCID prefix to scan from")
    b.add_argument("--include-untagged", action="store_true",
                   help="fetch: also take untagged PDFs (uses Adobe AutoTag quota on run)")
    b.add_argument("--no-ai", action="store_true", help="run: no Claude at all")
    b.add_argument("--alt-text", action="store_true",
                   help="run: also generate figure alt text (one Claude call per figure)")
    t = sub.add_parser("autotag", help="only run Adobe AutoTag")
    t.add_argument("pdf")
    t.add_argument("-o", "--output")
    t.add_argument("--adobe-report", action="store_true", help="also save Adobe's Excel report")
    s = sub.add_parser("setup", help="save API keys (hidden prompts)")
    s.add_argument("--status", action="store_true", help="show which keys are configured")
    s.add_argument("--anthropic-key")
    s.add_argument("--adobe-client-id")
    s.add_argument("--adobe-client-secret")
    sub.add_parser("app", help="open the drag-and-drop app in your browser")
    cp = sub.add_parser("corpus", help="real gov/college PDFs: fetch, then remediate + validate all")
    cp.add_argument("action", choices=["fetch", "run"])
    cp.add_argument("urls", nargs="*")
    cp.add_argument("--dir", default="benchmark/corpus")
    cp.add_argument("--ai", action="store_true",
                    help="use Claude (spends API credits on every document; off by default)")
    args = ap.parse_args(argv)

    if args.cmd == "app":
        from .app import main as app_main
        app_main([])
        return 0

    if args.cmd == "setup":
        if not args.status:
            given = {"anthropic_api_key": args.anthropic_key, "adobe_client_id": args.adobe_client_id,
                     "adobe_client_secret": args.adobe_client_secret}
            if any(given.values()):  # non-interactive: only set what was passed
                given = {k: (v or "") for k, v in given.items()}
            print(f"Saved to {credentials.setup(given)}")
        for name, state in credentials.status().items():
            print(f"  {name:20} {state}")
        return 0

    if args.cmd == "autotag":
        from .autotag import AutoTagClient
        from pathlib import Path
        src = Path(args.pdf)
        out = args.output or str(src.with_name(src.stem + "_autotagged.pdf"))
        rep = str(Path(out).with_suffix("")) + "_report.xlsx" if args.adobe_report else None
        cfg = config_mod.load()["autotag"]
        AutoTagClient(cfg["region"], cfg["timeout_seconds"]).autotag(
            str(src), out, report_dest=rep, shift_headings=cfg["shift_headings"])
        print(f"  -> {out}")
        if rep:
            print(f"  -> {rep}")
        return 0

    if args.cmd == "stats":
        from . import learning
        rows = learning.stats()
        if not rows:
            print("No decisions recorded yet. They're logged each time you run `apply`.")
        for key, c in rows.items():
            total = sum(c.values())
            print(f"  {key:22} {total:4} decisions   accepted {c.get('accepted', 0):3}   "
                  f"edited {c.get('edited', 0):3}   rejected {c.get('rejected', 0):3}   "
                  f"({round(100 * c.get('accepted', 0) / total)}% kept as-is)")
        hist = learning.eval_history()
        if hist:
            print("\nEval history (heading level accuracy / figure alt coverage):")
            for r in hist[-15:]:
                print(f"  {r['ts']}  {r['original'][:40]:40} {r['headings']['level_accuracy_pct']}% / "
                      f"{r['figures']['tool_alt_coverage_pct']}%  ai={r['ai']}")
        print(f"\nData: {learning.data_dir()}")
        return 0

    if args.cmd == "corpus":
        from . import corpus
        if args.action == "fetch":
            print(f"{corpus.fetch(args.urls, args.dir)} new document(s) in {args.dir}")
        else:
            s = corpus.run(args.dir, config_mod.load(), remediate, use_ai=args.ai)
            print(json.dumps(s, indent=1))
        return 0

    if args.cmd == "benchmark":
        from . import benchmark
        if args.action == "fetch":
            print(f"Fetching {args.n} open-licensed PMC articles into {args.dir} ...")
            got = benchmark.fetch(args.dir, args.n, args.start, tagged_only=not args.include_untagged)
            print(f"{got} article(s) ready. Next: python remediate.py benchmark run")
        else:
            cfg = config_mod.load()
            cfg["figures"]["ai_alt_text"] = args.alt_text
            s = benchmark.run(args.dir, cfg, remediate, use_ai=not args.no_ai)
            print(benchmark.format_summary(s))
        return 0

    if args.cmd == "eval":
        from .evaluate import format_result, run_eval
        result = run_eval(args.original, args.gold, config_mod.load(args.config), not args.no_ai,
                          remediate, compare_only=args.compare_only)
        print(format_result(result))
        return 0

    if args.cmd == "apply":
        learn = not args.no_learn and config_mod.load()["learning"]["record_decisions"]
        for line in apply_review(args.pdf, args.review, args.output, learn=learn):
            print(" ", line)
        print(f"Saved {args.output or args.pdf}. Re-run `check` on it, then PAC.")
        return 0

    cfg = config_mod.load(args.config)
    if getattr(args, "lang", None):
        cfg["language"] = args.lang
    files = list(iter_inputs(args.inputs))
    if not files:
        print("No PDFs found.")
        return 1
    worst = 0
    for f in files:
        t = time.time()
        print(f"\n== {f}")
        rep = remediate(f, cfg, out=getattr(args, "output", None) if len(files) == 1 else None,
                        use_ai=not getattr(args, "no_ai", False), only_check=args.cmd == "check",
                        force_autotag=getattr(args, "autotag", False))
        print(rep.summary_text())
        paths = output_paths(f, getattr(args, "output", None) if len(files) == 1 else None,
                             "_check" if args.cmd == "check" else "_accessible")
        if args.cmd == "run":
            print(f"  -> {paths['pdf']}\n  -> {paths['html']}\n  -> {paths['review']}")
        else:
            print(f"  -> {paths['html']}")
        print(f"  ({time.time() - t:.1f}s)")
        worst = max(worst, 1 if rep.remaining else 0)
    return worst
