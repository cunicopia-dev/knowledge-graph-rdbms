"""Raw-SQLite throughput probe — CPython's stdlib `sqlite3` binding.

One of three sibling scripts (python / node / bun) that run the *identical*
SQLite workload so you can see how much the language runtime's FFI overhead
costs — the storage engine is the same SQLite underneath all three. This is
NOT a kgrdbms benchmark; for that, see ../benchmark.py.

Emits one JSON line: insert + lookup throughput, summarized over repeats.
Driven by ../compare.py, or run standalone:

    python bench/runtimes/run_python.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import statistics
import sys
import tempfile
import time

N = int(os.environ.get("BENCH_N", "200000"))     # rows inserted per repeat
B = int(os.environ.get("BENCH_B", "50000"))      # lookups per timed block
R = int(os.environ.get("BENCH_R", "10"))         # repeats


def median(xs):
    return statistics.median(xs)


def new_db():
    c = sqlite3.connect(tempfile.mktemp(suffix=".db"))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    return c


# insert: per-call loop in one transaction (fair vs a JS for-loop)
insert_rates = []
many_rates = []
for _ in range(R):
    c = new_db()
    t = time.perf_counter_ns()
    c.execute("BEGIN")
    for i in range(N):
        c.execute("INSERT INTO t(id,v) VALUES(?,?)", (i, f"v{i}"))
    c.execute("COMMIT")
    insert_rates.append(N / ((time.perf_counter_ns() - t) / 1e9))
    # executemany: idiomatic Python, binding batched in C
    c.execute("DELETE FROM t"); c.commit()
    t = time.perf_counter_ns()
    c.executemany("INSERT INTO t(id,v) VALUES(?,?)", ((i, f"v{i}") for i in range(N)))
    c.commit()
    many_rates.append(N / ((time.perf_counter_ns() - t) / 1e9))
    c.close()

# lookups: time blocks of B point lookups (one timer pair per block)
c = new_db()
c.executemany("INSERT INTO t(id,v) VALUES(?,?)", ((i, f"v{i}") for i in range(N)))
c.commit()
lookup_rates = []
for _ in range(R):
    t = time.perf_counter_ns()
    s = 0
    for i in range(B):
        if c.execute("SELECT v FROM t WHERE id=?", (i,)).fetchone():
            s += 1
    lookup_rates.append(B / ((time.perf_counter_ns() - t) / 1e9))
c.close()

print(json.dumps({
    "runtime": f"CPython {sys.version.split()[0]}",
    "sqlite": sqlite3.sqlite_version,
    "insert_percall_ops": {"median": median(insert_rates), "max": max(insert_rates)},
    "insert_executemany_ops": {"median": median(many_rates), "max": max(many_rates)},
    "lookup_ops": {"median": median(lookup_rates), "max": max(lookup_rates)},
    "params": {"N": N, "B": B, "R": R},
}))
