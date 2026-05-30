#!/usr/bin/env python3
"""Render SQLite-vs-Postgres charts from the postgres benchmark data.

Pipeline:  bench/postgres/benchmark.py --json  ->  charts.py  ->  PNG in temp/

    # capture data (needs a reachable Postgres — see benchmark.py)
    python bench/postgres/benchmark.py --json > temp/pg.json
    python bench/postgres/charts.py --input temp/pg.json          # render
    python bench/postgres/charts.py                               # run + render

Reuses the palette + helpers from bench/charts.py so these match the project's
other figures. Three lenses on the same data:

  1. ratio     — diverging bars: who wins each op, and by how much (the summary)
  2. dumbbell  — absolute per-call latency, both engines, log scale (the gap)
  3. roundtrip — descendants vs shortest_path: same chain, opposite result (the WHY)

Outputs land in temp/ (gitignored).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
sys.path.insert(0, str(BENCH))            # reuse bench/charts.py styling
sys.path.insert(0, str(BENCH.parent))     # kgrdbms, if we run fresh

import charts as C  # noqa: E402  palette, setup_style, titled, footer, _save, human, _us
plt = C.plt


# ---- data ------------------------------------------------------------


def load(args) -> tuple[dict, dict, list]:
    if not (args.input and Path(args.input).exists()):
        raise SystemExit(
            f"no data at {args.input!r}. Capture it first:\n"
            f"  python bench/postgres/benchmark.py --json > temp/pg.json"
        )
    d = json.loads(Path(args.input).read_text())
    env, rows = d["environment"], d["results"]

    ops: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        eng, op = r["name"].split("] ", 1)
        eng = eng[1:]
        if op not in ops:
            ops[op] = {}
            order.append(op)
        ops[op][eng] = r
    return env, ops, order


def _clean(op: str) -> str:
    """Drop the bracketed annotation tag for axis labels."""
    return op.split("  [", 1)[0].split("  ←", 1)[0]


def _pg_footer(fig, env):
    p = env["params"]
    cap = (f"kgrdbms · SQLite {env['sqlite']} vs {env['postgres']} · "
           f"{p['scale']:,} nodes · chain {p['chain']}")
    fig.text(0.015, 0.02, cap, fontsize=8, color=C.MUTED, ha="left")
    fig.text(0.985, 0.02, C.REPO, fontsize=8, color=C.MUTED, ha="right")


# ---- 1. ratio: who wins, by how much --------------------------------


def chart_ratio(env, ops, order, outdir, fmts):
    rows = []
    for op in order:
        a, b = ops[op].get("sqlite"), ops[op].get("postgres")
        if a and b and a["p50_us"] > 0:
            rows.append((op, b["p50_us"] / a["p50_us"]))   # >1 → postgres slower
    rows.sort(key=lambda r: r[1])                            # pg-wins (small ratio) at bottom

    fig, ax = plt.subplots(figsize=(9.6, 5.4))
    fig.subplots_adjust(top=0.76, bottom=0.10, left=0.40, right=0.95)
    y = list(range(len(rows)))
    logs = [math.log10(r) for _, r in rows]
    colors = [C.ACCENT if l >= 0 else C.HILITE for l in logs]   # sqlite-faster vs pg-faster
    ax.barh(y, logs, color=colors, height=0.64, zorder=3)
    ax.axvline(0, color=C.INK, linewidth=1.1, zorder=4)
    ax.set_yticks(y)
    ax.set_yticklabels([_clean(op) for op, _ in rows], fontsize=9.5)

    for yi, (op, ratio), l in zip(y, rows, logs):
        txt = f"{ratio:.0f}× slower" if ratio >= 1 else f"{1/ratio:.1f}× faster"
        ha = "left" if l >= 0 else "right"
        ax.text(l + (0.06 if l >= 0 else -0.06), yi, txt, va="center", ha=ha,
                fontsize=10, fontweight="bold", color=(C.ACCENT if l >= 0 else C.HILITE))

    span = max(abs(v) for v in logs) * 1.5
    ax.set_xlim(-span, span)
    ax.set_xticks([])
    for s in ("top", "right", "bottom"):
        ax.spines[s].set_visible(False)
    ax.text(span, len(rows) - 0.25, "SQLite faster", ha="right", va="bottom",
            fontsize=10.5, color=C.ACCENT, fontweight="bold")
    ax.text(-span, len(rows) - 0.25, "Postgres faster", ha="left", va="bottom",
            fontsize=10.5, color=C.HILITE, fontweight="bold")

    fig.text(0.015, 0.945, "SQLite vs Postgres: who wins each operation",
             fontsize=16, fontweight="bold", color=C.INK, ha="left")
    fig.text(0.015, 0.895,
             "Per-call p50, log ratio. Embedded SQLite wins the small frequent ops; the lone "
             "exception is the recursive-CTE walk that runs server-side in one query.",
             fontsize=10, color=C.MUTED, ha="left")
    _pg_footer(fig, env)
    C._save(fig, outdir, "pg_ratio", fmts)


# ---- 2. dumbbell: absolute per-call latency -------------------------


def chart_dumbbell(env, ops, order, outdir, fmts):
    rows = [(op, ops[op]["sqlite"], ops[op]["postgres"]) for op in order
            if ops[op].get("sqlite", {}).get("category") == "latency" and "postgres" in ops[op]]
    rows.sort(key=lambda r: r[1]["p50_us"])

    fig, ax = plt.subplots(figsize=(9.6, 4.8))
    fig.subplots_adjust(top=0.78, bottom=0.14, left=0.31, right=0.93)
    ax.set_xscale("log")
    y = list(range(len(rows)))
    for yi, (op, a, b) in zip(y, rows):
        lo, hi = sorted((a["p50_us"], b["p50_us"]))
        ax.hlines(yi, lo, hi, color=C.SOFT, linewidth=3, zorder=2)
        ax.scatter(a["p50_us"], yi, s=85, color=C.ACCENT, zorder=4, label="SQLite" if yi == 0 else None)
        ax.scatter(b["p50_us"], yi, s=85, color=C.HILITE, zorder=4, label="Postgres" if yi == 0 else None)
        ratio = b["p50_us"] / a["p50_us"]
        mid = math.sqrt(lo * hi)
        tag = f"{ratio:.0f}×" if ratio >= 1 else f"{1/ratio:.1f}× (pg wins)"
        ax.text(mid, yi + 0.22, tag, ha="center", va="bottom", fontsize=8.5,
                color=(C.HILITE if ratio < 1 else C.MUTED), fontweight="bold")
        ax.text(a["p50_us"], yi - 0.28, C._us(a["p50_us"]), ha="center", va="top",
                fontsize=8, color=C.ACCENT)
        ax.text(b["p50_us"], yi - 0.28, C._us(b["p50_us"]), ha="center", va="top",
                fontsize=8, color=C.HILITE)
    ax.set_yticks(y)
    ax.set_yticklabels([_clean(op) for op, _, _ in rows], fontsize=9.5)
    ax.set_xlabel("latency per call — log scale (lower is better)")
    lows = [min(a["p50_us"], b["p50_us"]) for _, a, b in rows]
    highs = [max(a["p50_us"], b["p50_us"]) for _, a, b in rows]
    ax.set_xlim(min(lows) * 0.5, max(highs) * 2.4)
    ax.legend(loc="lower right", frameon=False, fontsize=10, handletextpad=0.3)
    C.titled(fig, ax, "The gap is the round-trip",
             "Each line spans the same op on both engines. In-process SQLite vs a localhost "
             "round-trip to Postgres — the line length is the tax.")
    _pg_footer(fig, env)
    C._save(fig, outdir, "pg_dumbbell", fmts)


# ---- 3. roundtrip: same chain, opposite result ----------------------


def chart_roundtrip(env, ops, order, outdir, fmts):
    # the two ops that walk the SAME chain: one CTE (1 query), one BFS (N queries)
    chain = env["params"]["chain"]
    pick = {}
    for op in order:
        if "descendants" in op:
            pick[f"descendants\n(recursive CTE · 1 query)"] = ops[op]
        elif "shortest_path" in op:
            pick[f"shortest_path\n(Python BFS · ~{chain} queries)"] = ops[op]
    labels = list(pick.keys())

    fig, ax = plt.subplots(figsize=(8.4, 5.2))
    fig.subplots_adjust(top=0.76, bottom=0.12, left=0.12, right=0.95)
    ax.set_yscale("log")
    x = range(len(labels))
    width = 0.34
    for i, eng, color in ((-1, "sqlite", C.ACCENT), (1, "postgres", C.HILITE)):
        vals = [pick[l][eng]["p50_us"] for l in labels]
        xs = [xi + i * width / 2 for xi in x]
        ax.bar(xs, vals, width=width * 0.92, color=color, zorder=3,
               label=eng.capitalize())
        for xi, v in zip(xs, vals):
            ax.text(xi, v * 1.12, C._us(v), ha="center", va="bottom",
                    fontsize=9.5, fontweight="bold", color=color)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, fontsize=10.5)
    ax.set_ylabel("p50 latency — log scale (lower is better)")
    ax.legend(loc="upper left", frameon=False, fontsize=10)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(axis="y", color=C.GRID, linewidth=1.0, zorder=0)
    ax.set_axisbelow(True)

    fig.text(0.015, 0.945, f"Same {env['params']['chain']}-deep chain, opposite verdict",
             fontsize=16, fontweight="bold", color=C.INK, ha="left")
    fig.text(0.015, 0.895,
             "Both walk the identical chain. As one server-side query Postgres wins; as a "
             "per-hop BFS it pays the round-trip every hop. The engine didn't change — the "
             "query count did.", fontsize=10, color=C.MUTED, ha="left")
    _pg_footer(fig, env)
    C._save(fig, outdir, "pg_roundtrip", fmts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Render SQLite-vs-Postgres charts.")
    ap.add_argument("--input", default=str(BENCH.parent / "temp" / "pg.json"),
                    help="postgres benchmark JSON (default: temp/pg.json)")
    ap.add_argument("--outdir", default=str(BENCH.parent / "temp"))
    ap.add_argument("--svg", action="store_true")
    args = ap.parse_args(argv)

    C.setup_style()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    fmts = ["png"] + (["svg"] if args.svg else [])

    env, ops, order = load(args)
    chart_ratio(env, ops, order, outdir, fmts)
    chart_dumbbell(env, ops, order, outdir, fmts)
    chart_roundtrip(env, ops, order, outdir, fmts)
    print(f"\ncharts in {outdir}/  (gitignored)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
