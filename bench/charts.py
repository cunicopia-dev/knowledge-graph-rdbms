#!/usr/bin/env python3
"""Render publication-quality charts from kgrdbms benchmark data.

Pipeline:  benchmark.py --json  ->  charts.py  ->  PNG/SVG in temp/ (gitignored)

    python bench/charts.py                              # auto-run + render all
    python bench/charts.py --input temp/results.json    # reuse saved data
    python bench/charts.py --no-runtimes --svg          # skip cross-runtime, also emit SVG

Needs the charts extra:
    pip install "knowledge-graph-rdbms[charts]"     # (matplotlib)

Outputs land in temp/ by default, which is gitignored. When a chart is good
enough for the README, promote it: move it into assets/ and commit that.

Styling lives in STYLE / palette constants below — tweak freely; that's the
"make it really good later" surface.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                  # benchmark.py
sys.path.insert(0, str(HERE / "runtimes"))     # compare.py

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter
except ImportError:
    sys.exit("charts need matplotlib:  pip install 'knowledge-graph-rdbms[charts]'")


# ---- palette + style -------------------------------------------------

INK = "#1b1f24"      # near-black text
MUTED = "#8b95a3"    # secondary text / axes
GRID = "#eaedf1"     # gridlines
ACCENT = "#2f6f8f"   # primary bars (teal-blue)
HILITE = "#e07a3f"   # the "hero" / highlight bar (warm orange)
SOFT = "#c7d2da"     # de-emphasized bars
RUNTIME = {"CPython": "#3776ab", "Node": "#3c873a", "Bun": "#f06aa6"}

REPO = "github.com/cunicopia-dev/knowledge-graph-rdbms"


def setup_style():
    plt.rcParams.update({
        "savefig.dpi": 200,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "font.family": "sans-serif",
        "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
        "font.size": 11,
        "text.color": INK,
        "axes.edgecolor": MUTED,
        "axes.linewidth": 0.8,
        "axes.labelcolor": INK,
        "xtick.color": MUTED,
        "ytick.color": INK,
        "ytick.labelsize": 10.5,
    })


def human(n: float) -> str:
    if n >= 1e6:
        return f"{n/1e6:.2f}M"
    if n >= 1e3:
        return f"{n/1e3:.0f}k"
    return f"{n:.0f}"


def short_platform(env: dict) -> str:
    plat = env.get("platform", "")
    proc = env.get("processor", "")
    # macOS-26.3-arm64-arm-64bit-Mach-O  ->  macOS · arm64
    head = plat.split("-")[0] if plat else ""
    return f"{head} · {proc}".strip(" ·")


def titled(fig, ax, title: str, subtitle: str):
    fig.text(0.015, 0.945, title, fontsize=16, fontweight="bold", color=INK, ha="left")
    fig.text(0.015, 0.895, subtitle, fontsize=10.5, color=MUTED, ha="left")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(axis="x", color=GRID, linewidth=1.0, zorder=0)
    ax.set_axisbelow(True)


def footer(fig, env: dict):
    cap = f"kgrdbms · {env['implementation']} {env['python']} · SQLite {env['sqlite']} · {short_platform(env)}"
    fig.text(0.015, 0.02, cap, fontsize=8, color=MUTED, ha="left")
    fig.text(0.985, 0.02, REPO, fontsize=8, color=MUTED, ha="right")


# ---- charts ----------------------------------------------------------


def chart_write_throughput(results, env, outdir, fmts):
    """Horizontal bars of sustained write throughput — the batching story."""
    wanted = [
        "add_node (per-call commit)",
        "EventLog.record (batched)",
        "service.upsert_node (gated+logged, batched)",
        "add_edges() bulk",
        "add_node inside batch()",
        "add_nodes() bulk",
    ]
    by_name = {r["name"]: r for r in results if r["category"] == "throughput"}
    rows = [by_name[n] for n in wanted if n in by_name]
    rows.sort(key=lambda r: r["ops_per_s"])
    labels = [r["name"].replace(" (per-call commit)", "\n(per-call commit)")
              .replace("service.upsert_node (gated+logged, batched)", "service.upsert_node\n(gated + logged)")
              for r in rows]
    vals = [r["ops_per_s"] for r in rows]

    fig, ax = plt.subplots(figsize=(9.2, 5.2))
    fig.subplots_adjust(top=0.80, bottom=0.12, left=0.30, right=0.95)
    colors = []
    for r in rows:
        if "per-call commit" in r["name"]:
            colors.append(SOFT)
        elif r["name"] == "add_nodes() bulk":
            colors.append(HILITE)
        else:
            colors.append(ACCENT)
    y = range(len(rows))
    ax.barh(list(y), vals, color=colors, height=0.66, zorder=3)
    ax.set_yticks(list(y))
    ax.set_yticklabels(labels)
    ax.set_xlabel("operations / second (higher is better)")
    ax.set_xlim(0, max(vals) * 1.16)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: human(v) if v else "0"))
    for yi, v in zip(y, vals):
        ax.text(v + max(vals) * 0.012, yi, human(v) + "/s", va="center", ha="left",
                fontsize=10.5, fontweight="bold", color=INK)

    percall = by_name["add_node (per-call commit)"]["ops_per_s"]
    batched = by_name["add_node inside batch()"]["ops_per_s"]
    titled(fig, ax, "Writes: batch the commit, go ~10× faster",
           f"The same add_node call: {human(percall)}/s committing per call vs "
           f"{human(batched)}/s inside batch() — one transaction, not N.")
    footer(fig, env)
    _save(fig, outdir, "write_throughput", fmts)


def chart_read_latency(results, env, outdir, fmts):
    """Dumbbell plot: marker at p50, whisker to p99, log x — the tail story."""
    rows = [r for r in results if r["category"] == "latency"]
    rows.sort(key=lambda r: r["p50_us"])
    labels = [r["name"] for r in rows]

    fig, ax = plt.subplots(figsize=(9.2, 5.0))
    fig.subplots_adjust(top=0.80, bottom=0.14, left=0.27, right=0.95)
    ax.set_xscale("log")
    y = list(range(len(rows)))
    for yi, r in zip(y, rows):
        ax.hlines(yi, r["p50_us"], r["p99_us"], color=SOFT, linewidth=3, zorder=2)
        ax.scatter(r["p50_us"], yi, s=70, color=ACCENT, zorder=4, label="p50" if yi == 0 else None)
        ax.scatter(r["p90_us"], yi, s=34, color=MUTED, zorder=4, label="p90" if yi == 0 else None)
        ax.scatter(r["p99_us"], yi, s=46, color=HILITE, zorder=4, marker="D", label="p99" if yi == 0 else None)
        ax.text(r["p50_us"] * 0.82, yi, _us(r["p50_us"]), va="center", ha="right", fontsize=9, color=ACCENT)
        ax.text(r["p99_us"] * 1.2, yi, _us(r["p99_us"]), va="center", ha="left", fontsize=9, color=HILITE)
    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    ax.set_xlabel("latency per call — log scale (lower is better)")
    ax.set_xlim(min(r["p50_us"] for r in rows) * 0.45, max(r["p99_us"] for r in rows) * 2.6)
    ax.legend(loc="lower right", frameon=False, fontsize=9.5, ncol=3, handletextpad=0.2, columnspacing=1.0)

    titled(fig, ax, "Reads: fast, with an honest tail",
           "Marker at p50, whisker to p99. The spread is the workload — averages hide it.")
    footer(fig, env)
    _save(fig, outdir, "read_latency", fmts)


def chart_runtimes(runtimes, env, outdir, fmts):
    """Grouped bars: same SQLite across CPython / Node / Bun."""
    if not runtimes:
        return
    def fam(rt):  # "CPython 3.14.2" -> "CPython"
        return rt.split()[0]
    order = ["CPython", "Node", "Bun"]
    present = [r for r in runtimes if fam(r["runtime"]) in order]
    present.sort(key=lambda r: order.index(fam(r["runtime"])))

    metrics = [("insert/call", "insert_percall_ops"), ("lookup", "lookup_ops")]
    fig, ax = plt.subplots(figsize=(9.2, 5.0))
    fig.subplots_adjust(top=0.80, bottom=0.12, left=0.10, right=0.96)
    n = len(present)
    width = 0.78 / max(n, 1)
    xbase = range(len(metrics))
    for i, r in enumerate(present):
        fam_name = fam(r["runtime"])
        color = RUNTIME.get(fam_name, ACCENT)
        vals = []
        for _, key in metrics:
            m = r.get(key) or {}
            vals.append(m.get("median", 0))
        xs = [x + (i - (n - 1) / 2) * width for x in xbase]
        bars = ax.bar(xs, vals, width=width * 0.92, color=color, zorder=3,
                      label=f"{fam_name} (SQLite {r.get('sqlite','?')})")
        for x, v in zip(xs, vals):
            ax.text(x, v + max(v, 1) * 0.02, human(v), ha="center", va="bottom",
                    fontsize=9, fontweight="bold", color=INK)
    ax.set_xticks(list(xbase))
    ax.set_xticklabels([m[0] for m in metrics])
    ax.set_ylabel("operations / second")
    ax.set_ylim(0, max((r.get(k) or {}).get("median", 0)
                       for r in present for _, k in metrics) * 1.18)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: human(v) if v else "0"))
    ax.legend(loc="upper right", frameon=False, fontsize=9.5)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(axis="y", color=GRID, linewidth=1.0, zorder=0)
    ax.set_axisbelow(True)

    titled(fig, ax, "Same SQLite, different runtime",
           "Raw-SQLite throughput across CPython, Node, and Bun — the spread is pure binding overhead (<2×).")
    footer(fig, env)
    _save(fig, outdir, "runtimes", fmts)


def chart_crossover(report, outdir, fmts):
    """Diverging bars: kgrdbms vs Neo4j per-call p50 — where embedded loses to a
    real graph engine. Consumes bench/neo4j/headtohead.py --json output."""
    rows = list(reversed(report["results"]))   # shallow ops on top
    ops = [r["op"] for r in rows]
    ratios = [r["ratio_p50"] for r in rows]     # >1 → kgrdbms faster
    logs = [math.log10(r) for r in ratios]

    fig, ax = plt.subplots(figsize=(9.4, 4.8))
    fig.subplots_adjust(top=0.78, bottom=0.10, left=0.24, right=0.95)
    y = list(range(len(rows)))
    colors = [ACCENT if l >= 0 else HILITE for l in logs]
    ax.barh(y, logs, color=colors, height=0.62, zorder=3)
    ax.axvline(0, color=INK, linewidth=1.1, zorder=4)
    ax.set_yticks(y)
    ax.set_yticklabels(ops)

    for yi, r, l in zip(y, rows, logs):
        rt = r["ratio_p50"]
        txt = f"{rt:.0f}× faster" if rt >= 1 else f"{1/rt:.0f}× slower"
        ha = "left" if l >= 0 else "right"
        ax.text(l + (0.05 if l >= 0 else -0.05), yi, txt, va="center", ha=ha,
                fontsize=10.5, fontweight="bold", color=(ACCENT if l >= 0 else HILITE))

    span = max(abs(v) for v in logs) * 1.45
    ax.set_xlim(-span, span)
    ax.set_xticks([])
    for s in ("top", "right", "bottom"):
        ax.spines[s].set_visible(False)
    ax.text(span, len(rows) - 0.3, "kgrdbms wins", ha="right", va="bottom",
            fontsize=10.5, color=ACCENT, fontweight="bold")
    ax.text(-span, len(rows) - 0.3, "Neo4j wins", ha="left", va="bottom",
            fontsize=10.5, color=HILITE, fontweight="bold")

    p = report.get("params", {})
    fig.text(0.015, 0.945, "Where the crossover is: embedded vs. server",
             fontsize=16, fontweight="bold", color=INK, ha="left")
    fig.text(0.015, 0.895,
             "kgrdbms (in-process SQLite) vs Neo4j (Bolt server), per-call p50 latency. "
             "Shallow ops favor no round-trip; deep traversal favors index-free adjacency.",
             fontsize=10, color=MUTED, ha="left")
    fig.text(0.015, 0.02, f"kgrdbms · vs Neo4j 5 · {p.get('scale', '?'):,} nodes · "
             f"chain {p.get('chain', '?')}", fontsize=8, color=MUTED, ha="left")
    fig.text(0.985, 0.02, REPO, fontsize=8, color=MUTED, ha="right")
    _save(fig, outdir, "crossover", fmts)


# ---- helpers ---------------------------------------------------------


def _us(us: float) -> str:
    if us >= 1000:
        return f"{us/1000:.1f}ms"
    return f"{us:.1f}µs"


def _save(fig, outdir: Path, name: str, fmts):
    for fmt in fmts:
        path = outdir / f"{name}.{fmt}"
        fig.savefig(path, bbox_inches="tight", facecolor="white")
        print(f"  wrote {path}")
    plt.close(fig)


def load_benchmark(args):
    if args.input and Path(args.input).exists():
        d = json.loads(Path(args.input).read_text())
        return d["environment"], d["results"]
    import benchmark
    ns = argparse.Namespace(scale=args.scale, iterations=args.iterations,
                            repeats=args.repeats, seed=args.seed, json=True)
    print("  running benchmark for fresh data…", file=sys.stderr)
    results = [asdict(r) for r in benchmark.run(ns)]
    return benchmark.environment(ns), results


def load_runtimes():
    import compare
    out = []
    for name, cmd in compare.runners():
        print(f"  probing {name}…", file=sys.stderr)
        res = compare.run_one(cmd)
        if res and "error" not in res:
            out.append(res)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Render charts from kgrdbms benchmark data.")
    ap.add_argument("--input", help="benchmark JSON (from benchmark.py --json); omit to run fresh")
    ap.add_argument("--outdir", default=str(HERE.parent / "temp"), help="output dir (default: temp/)")
    ap.add_argument("--svg", action="store_true", help="also emit SVG (vector, crisp for READMEs)")
    ap.add_argument("--no-runtimes", action="store_true", help="skip the cross-runtime chart")
    ap.add_argument("--neo4j", help="head-to-head JSON (from bench/neo4j/headtohead.py --json) → crossover chart")
    ap.add_argument("--only-neo4j", action="store_true", help="render only the crossover chart")
    ap.add_argument("--scale", type=int, default=10_000)
    ap.add_argument("--iterations", type=int, default=20_000)
    ap.add_argument("--repeats", type=int, default=7)
    ap.add_argument("--seed", type=int, default=1234)
    args = ap.parse_args(argv)

    setup_style()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    fmts = ["png"] + (["svg"] if args.svg else [])

    if args.neo4j:
        chart_crossover(json.loads(Path(args.neo4j).read_text()), outdir, fmts)
        if args.only_neo4j:
            print(f"\ncharts in {outdir}/")
            return 0

    env, results = load_benchmark(args)
    chart_write_throughput(results, env, outdir, fmts)
    chart_read_latency(results, env, outdir, fmts)
    if not args.no_runtimes:
        chart_runtimes(load_runtimes(), env, outdir, fmts)

    print(f"\ncharts in {outdir}/  (gitignored — promote keepers into assets/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
