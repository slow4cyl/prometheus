#!/usr/bin/env python3
"""backfill_calibration_mv.py — recompute worker_results.calibrated_confidence
for the WHOLE table using the learned multivariate model (with full evidence
counts joined from knowledge_claims).

Differs from the write-time scorer: at write time evidence counts aren't known
yet (default 0), but for backfill we have the accumulated support/refute/contra
from the joined knowledge_claim, so historical rows get the full-signal value.

Safe for the LIVE system: WAL, busy_timeout, synchronous=NORMAL, chunked commits
with BEGIN IMMEDIATE. Rows with confidence IS NULL stay NULL. If the model can't
be loaded, aborts WITHOUT touching anything (the scalar values already in place
remain valid). Idempotent.
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
from calibration_runtime import calibrate_mv, model_available  # noqa: E402
from write_worker_result import calibrate_confidence as scalar_cal  # noqa: E402

DB = os.path.expanduser("~/.hermes/prometheus.db")
CHUNK = 2000


def main():
    if not model_available():
        print("ABORT: no multivariate model loaded; leaving existing values intact.")
        return 1

    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    con.execute("PRAGMA synchronous=NORMAL")
    cur = con.cursor()

    # Pull each row's raw confidence + type signals + joined evidence counts.
    #
    # JOIN-FIX (2026-06-20): source evidence counts via claim_evidence, NOT via
    # knowledge_claims.first_experiment_id. A claim accumulates evidence from many
    # experiments over time; the old join (kc.first_experiment_id = wr.experiment_id)
    # only matched a worker_result when its experiment happened to be the claim's
    # FIRST experiment, so every later evidence row got support=refute=contra=0 and
    # was scored as if the claim had zero support — crushing calibrated_confidence
    # to near-zero for ~2,000 genuinely-supported rows (net_support is the model's
    # strongest feature). The correct path is worker_result -> claim_evidence ->
    # knowledge_claims. A worker_result can be evidence for >1 claim, so we take the
    # MAX support/refute/contra across the claims it supports (dedup the fan-out).
    cur.execute("""
        WITH ev AS (
            SELECT ce.worker_result_id AS wrid,
                   MAX(COALESCE(kc.support_count,0))       AS sup,
                   MAX(COALESCE(kc.refute_count,0))        AS ref,
                   MAX(COALESCE(kc.contradiction_count,0)) AS con
            FROM claim_evidence ce
            JOIN knowledge_claims kc ON kc.id = ce.claim_id
            WHERE ce.worker_result_id IS NOT NULL
            GROUP BY ce.worker_result_id
        )
        SELECT wr.id, wr.confidence, wr.experiment_type, wr.mechanism_type,
               wr.artifact_status,
               COALESCE(ev.sup,0),
               COALESCE(ev.ref,0),
               COALESCE(ev.con,0)
        FROM worker_results wr
        LEFT JOIN ev ON ev.wrid = wr.id
        WHERE wr.confidence IS NOT NULL
    """)
    work = cur.fetchall()
    total = len(work)
    print(f"rows to recalibrate (multivariate): {total}")

    written = 0
    fell_back = 0
    t0 = time.time()
    for i in range(0, total, CHUNK):
        chunk = work[i:i + CHUNK]
        updates = []
        for rid, conf, et, mt, art, sup, ref, contra in chunk:
            # Coerce confidence to float — some rows historically stored text
            # labels ("HIGH", "low") which crash the scalar fallback's round().
            try:
                conf = float(conf)
            except (TypeError, ValueError):
                continue  # skip non-numeric confidence rows
            val = calibrate_mv(conf, experiment_type=et, mechanism_type=mt,
                               artifact_status=art, support_count=sup,
                               refute_count=ref, contradiction_count=contra)
            if val is None:  # model hiccup on a row -> scalar fallback, never crash
                val = scalar_cal(conf)
                fell_back += 1
            updates.append((val, rid))
        cur.execute("BEGIN IMMEDIATE")
        cur.executemany(
            "UPDATE worker_results SET calibrated_confidence=? WHERE id=?", updates)
        con.commit()
        written += len(updates)
        if (i // CHUNK) % 5 == 0 or written >= total:
            print(f"  committed {written}/{total} ({100*written/total:.1f}%) "
                  f"elapsed={time.time()-t0:.1f}s", flush=True)

    con.close()
    print(f"DONE: {written} rows recalibrated in {time.time()-t0:.1f}s "
          f"(scalar fallback on {fell_back} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
