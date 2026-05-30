#!/usr/bin/env python3
"""Cross-runtime raw-SQLite comparison: CPython vs Node vs Bun.

Runs the three sibling probes (run_python.py / run_node.mjs / run_bun.js) over
the *identical* SQLite workload and tabulates them. The point isn't to crown a
runtime — it's to show that since the storage engine is the same SQLite in all
three, the spread is pure language/binding FFI overhead (and is small).

This is an optional appendix, not a kgrdbms benchmark. It only runs the
runtimes you actually have installed; missing ones are skipped.

    python bench/runtimes/compare.py
    BENCH_N=500000 BENCH_R=15 python bench/runtimes/compare.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _bun() -> str | None:
    return shutil.which("bun") or (str(Path.home() / ".bun/bin/bun")
                                   if (Path.home() / ".bun/bin/bun").exists() else None)


def runners() -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = [("python", [sys.executable, str(HERE / "run_python.py")])]
    if shutil.which("node"):
        out.append(("node", ["node", "--experimental-sqlite", str(HERE / "run_node.mjs")]))
    if _bun():
        out.append(("bun", [_bun(), "run", str(HERE / "run_bun.js")]))
    return out


def run_one(cmd: list[str]) -> dict | None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ}, timeout=600)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    # the probe prints a single JSON line to stdout (last non-empty line)
    for line in reversed(proc.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"error": (proc.stderr.strip() or "no JSON output")[:200]}


def fmt(n) -> str:
    return f"{n:,.0f}" if isinstance(n, (int, float)) else str(n)


def main() -> int:
    params = {k: os.environ.get(k) for k in ("BENCH_N", "BENCH_B", "BENCH_R") if os.environ.get(k)}
    print("cross-runtime raw-SQLite comparison (same engine; measures binding overhead)")
    print(f"  params: N={os.environ.get('BENCH_N', '200000')} inserts · "
          f"B={os.environ.get('BENCH_B', '50000')} lookups/block · "
          f"R={os.environ.get('BENCH_R', '10')} repeats")
    print()
    rows = []
    for name, cmd in runners():
        print(f"  running {name}…", file=sys.stderr)
        res = run_one(cmd)
        rows.append((name, res))

    hdr = f"  {'runtime':<16}{'SQLite':<10}{'insert/call':>14}{'insert/many':>14}{'lookup':>14}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for name, res in rows:
        if not res or "error" in res:
            print(f"  {name:<16}{'—':<10}{'(failed: ' + (res or {}).get('error', '?')[:30] + ')':>40}")
            continue
        ins = res.get("insert_percall_ops", {}).get("median", 0)
        many = res.get("insert_executemany_ops", {}).get("median")
        look = (res.get("lookup_ops") or {}).get("median", 0)
        print(f"  {res['runtime']:<16}{res.get('sqlite', '?'):<10}"
              f"{fmt(ins):>14}{(fmt(many) if many else '—'):>14}{fmt(look):>14}")
        if res.get("note"):
            print(f"  {'':<16}↳ {res['note']}")
    print()
    print("  insert/call = per-call loop in one txn · insert/many = executemany (Python only)")
    print("  lookup = point lookups/sec · all medians over R repeats · higher is better")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
