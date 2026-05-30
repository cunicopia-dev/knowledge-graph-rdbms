// Raw-SQLite throughput probe — Bun's bun:sqlite binding.
// Sibling of run_python.py / run_node.mjs. Same SQLite engine; measures FFI cost.
// Run: bun run bench/runtimes/run_bun.js
import { Database } from 'bun:sqlite';
import { tmpdir } from 'node:os'; import { join } from 'node:path'; import { unlinkSync } from 'node:fs';
const N = +(process.env.BENCH_N || 200000), B = +(process.env.BENCH_B || 50000), R = +(process.env.BENCH_R || 10);
const median = (a) => { const s = [...a].sort((x, y) => x - y); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
function newdb() {
  const p = join(tmpdir(), 'b' + Date.now() + '_' + Math.floor(Math.random() * 1e9) + '.db');
  const db = new Database(p);
  db.exec('PRAGMA journal_mode=WAL'); db.exec('PRAGMA synchronous=NORMAL');
  db.run('CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)');
  return { db, p };
}
const insert = [];
for (let r = 0; r < R; r++) {
  const { db, p } = newdb();
  const ins = db.prepare('INSERT INTO t(id,v) VALUES(?,?)');
  const t = performance.now();
  db.exec('BEGIN'); for (let i = 0; i < N; i++) ins.run(i, 'v' + i); db.exec('COMMIT');
  insert.push(N / ((performance.now() - t) / 1000)); db.close(); unlinkSync(p);
}
const { db, p } = newdb();
const seed = db.prepare('INSERT INTO t(id,v) VALUES(?,?)');
db.exec('BEGIN'); for (let i = 0; i < N; i++) seed.run(i, 'v' + i); db.exec('COMMIT');
const lookup = [];
for (let r = 0; r < R; r++) {
  const sel = db.prepare('SELECT v FROM t WHERE id=?');
  const t = performance.now(); let s = 0;
  for (let i = 0; i < B; i++) { if (sel.get(i)) s++; }
  lookup.push(B / ((performance.now() - t) / 1000));
}
const ver = db.query('select sqlite_version() v').get().v;
db.close(); unlinkSync(p);
console.log(JSON.stringify({
  runtime: 'Bun ' + Bun.version, sqlite: ver,
  insert_percall_ops: { median: median(insert), max: Math.max(...insert) },
  lookup_ops: { median: median(lookup), max: Math.max(...lookup) },
  params: { N, B, R },
}));
