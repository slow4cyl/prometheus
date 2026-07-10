"""Busy-board load harness for the torn-extend tripwire.

Reproduces the falsification setup from TORN_EXTEND_NOTES.md: concurrent
writers inserting ~3.5KB bodies (multi-page growth commits → benign
deficit=K>=2 windows) under wal_autocheckpoint=100 churn, plus a reader to
force partial backfills. The disproven constant-1 tolerance raised 79x in
20s here; the robust check must raise 0 times — and must still raise on a
real truncation afterwards.

Usage:
  python3 torn_harness.py            # parent: spawns writers+reader, aggregates
  python3 torn_harness.py writer N   # child roles (internal)
  python3 torn_harness.py reader N
"""
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, '/home/goose/.hermes/hermes-agent')

WORK = pathlib.Path('/home/goose/.hermes/tmp/claude-work')
DB = WORK / 'torn-harness.db'
DURATION_S = 25
N_WRITERS = 4
BODY = 'x' * 3500


def writer(tag: str) -> None:
    import hermes_cli.kanban_db as kb
    stats = {
        'role': 'writer', 'tag': tag, 'commits': 0, 'alarms': [],
        'check_calls': 0, 'checks_gt_0_5ms': 0, 'checks_gt_2ms': 0,
        'checks_gt_16ms': 0, 'max_check_ms': 0.0,
    }
    real_check = kb._check_file_length_invariant

    def timed_check(conn):
        t0 = time.perf_counter()
        try:
            real_check(conn)
        finally:
            dt_ms = (time.perf_counter() - t0) * 1000.0
            stats['check_calls'] += 1
            stats['max_check_ms'] = max(stats['max_check_ms'], dt_ms)
            if dt_ms > 0.5:
                stats['checks_gt_0_5ms'] += 1
            if dt_ms > 2:
                stats['checks_gt_2ms'] += 1
            if dt_ms > 16:
                stats['checks_gt_16ms'] += 1

    kb._check_file_length_invariant = timed_check
    conn = kb.connect(db_path=DB)
    deadline = time.monotonic() + DURATION_S
    i = 0
    while time.monotonic() < deadline:
        i += 1
        try:
            with kb.write_txn(conn) as c:
                c.execute(
                    "INSERT INTO tasks (id, title, body, assignee, status, priority, created_at) "
                    "VALUES (?, ?, ?, 'tester', 'todo', 0, 1)",
                    (f"t_{tag}_{i:07d}", f"load {tag} {i}", BODY),
                )
            stats['commits'] += 1
        except sqlite3.DatabaseError as exc:
            stats['alarms'].append(str(exc)[:160])
    conn.close()
    print(json.dumps(stats), flush=True)


def reader(tag: str) -> None:
    import hermes_cli.kanban_db as kb
    conn = kb.connect(db_path=DB)
    deadline = time.monotonic() + DURATION_S
    reads = 0
    while time.monotonic() < deadline:
        conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
        reads += 1
        time.sleep(0.002)
    conn.close()
    print(json.dumps({'role': 'reader', 'tag': tag, 'reads': reads}), flush=True)


def parent() -> None:
    for suf in ('', '-wal', '-shm'):
        try:
            os.remove(str(DB) + suf)
        except FileNotFoundError:
            pass
    env = dict(os.environ, HERMES_HOME=str(WORK / 'test-home'))
    procs = [
        subprocess.Popen([sys.executable, __file__, 'writer', str(n)],
                         stdout=subprocess.PIPE, text=True, env=env)
        for n in range(N_WRITERS)
    ] + [subprocess.Popen([sys.executable, __file__, 'reader', '0'],
                          stdout=subprocess.PIPE, text=True, env=env)]
    outs = [p.communicate()[0] for p in procs]
    total_commits = total_alarms = total_calls = 0
    slow_05 = slow_2 = slow_16 = 0
    max_ms = 0.0
    for out in outs:
        line = out.strip().splitlines()[-1]
        s = json.loads(line)
        if s['role'] == 'writer':
            total_commits += s['commits']
            total_alarms += len(s['alarms'])
            total_calls += s['check_calls']
            slow_05 += s['checks_gt_0_5ms']
            slow_2 += s['checks_gt_2ms']
            slow_16 += s['checks_gt_16ms']
            max_ms = max(max_ms, s['max_check_ms'])
            for a in s['alarms']:
                print("ALARM:", a)
        else:
            print("reader reads:", s['reads'])
    print(f"writers={N_WRITERS} duration={DURATION_S}s commits={total_commits} "
          f"check_calls={total_calls} false_alarms={total_alarms}")
    print(f"check latency: >0.5ms={slow_05} >2ms={slow_2} >16ms(drain path)={slow_16} max={max_ms:.2f}ms")

    import hermes_cli.kanban_db as kb
    conn = kb.connect(db_path=DB)
    ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
    n = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    conn.close()
    print(f"integrity_check={ic} rows={n}")

    # Now the positive control: real truncation with an empty WAL must raise.
    with open(DB, 'r+b') as f:
        f.truncate(os.path.getsize(DB) - page_size)
    raw = sqlite3.connect(str(DB), isolation_level=None)
    try:
        kb._check_file_length_invariant(raw)
        print("POSITIVE-CONTROL: FAILED (no raise on real truncation)")
    except sqlite3.DatabaseError as exc:
        print(f"POSITIVE-CONTROL: raised as required: {str(exc)[:120]}")
    raw.close()
    print("HARNESS-VERDICT:", "PASS" if total_alarms == 0 else "FAIL")


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'writer':
        writer(sys.argv[2])
    elif len(sys.argv) > 1 and sys.argv[1] == 'reader':
        reader(sys.argv[2])
    else:
        parent()
