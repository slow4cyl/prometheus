"""Fragment-merge migration: re-point, tombstone, rollback, recompute-skip."""
import sqlite3

import merge_duplicate_claims as mdc
import maturity


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE knowledge_claims (
            id INTEGER PRIMARY KEY, claim_hash TEXT, hypothesis_text TEXT,
            claim_status TEXT, status TEXT, created_at TEXT);
        CREATE TABLE claim_evidence (
            id INTEGER PRIMARY KEY, claim_id INTEGER, worker_result_id INTEGER,
            key_finding TEXT,
            UNIQUE(claim_id, worker_result_id), UNIQUE(claim_id, key_finding));
        CREATE TABLE adversarial_replications (id INTEGER PRIMARY KEY, claim_id INTEGER);
        CREATE TABLE claim_scopes (id INTEGER PRIMARY KEY, claim_id INTEGER, source_curiosity_id INTEGER,
            UNIQUE(claim_id, source_curiosity_id));
        CREATE TABLE answer_adjudications (id INTEGER PRIMARY KEY, claim_id INTEGER);
        CREATE TABLE circularity_reviews (id INTEGER PRIMARY KEY, claim_id INTEGER);
        CREATE TABLE meta_transfer_predictions (id INTEGER PRIMARY KEY, meta_claim_id INTEGER);
    """)
    return conn


def _seed_group(conn):
    # Three claims that normalize to the same hash (retest tags differ), plus a
    # distinct claim that must never be touched.
    hyp = "Does injection detection transfer to anomaly detection?"
    gh = mdc._norm_hash(hyp)
    conn.execute("INSERT INTO knowledge_claims VALUES (1,?,?,?,?,?)",
                 (gh, f"[TRANSFER] {hyp} (10% success, 3 transfers)", "REPLICATED", "ACTIVE", "100"))
    conn.execute("INSERT INTO knowledge_claims VALUES (2,'stale2',?,?,?,?)",
                 (f"[TRANSFER] {hyp} (40% success, 9 transfers)", "ESTABLISHED", "ACTIVE", "90"))
    conn.execute("INSERT INTO knowledge_claims VALUES (3,'stale3',?,?,?,?)",
                 (f"{hyp} (SR=0.2, N=5, routing=x)", "CANDIDATE", "ACTIVE", "80"))
    conn.execute("INSERT INTO knowledge_claims VALUES (9,'other','unrelated Q','ESTABLISHED','ACTIVE','50')")
    # evidence: loser 2 has a worker_result_id collision with survivor 1, loser 3 is clean
    conn.executemany("INSERT INTO claim_evidence (claim_id, worker_result_id, key_finding) VALUES (?,?,?)",
                     [(1, 100, "s-a"), (2, 100, "l-collide"), (2, 201, "l-b"), (3, 300, "l-c")])
    conn.execute("INSERT INTO adversarial_replications (claim_id) VALUES (2)")
    conn.execute("INSERT INTO meta_transfer_predictions (meta_claim_id) VALUES (3)")
    conn.commit()
    return gh


def test_plan_picks_canonical_hash_survivor():
    conn = _db(); gh = _seed_group(conn)
    plan = mdc.build_plan(conn)
    assert set(plan) == {gh}
    # claim 1 owns the canonical hash -> survivor even though 2 is higher tier
    assert plan[gh]["survivor"]["id"] == 1
    assert sorted(plan[gh]["loser_ids"]) == [2, 3]
    assert plan[gh]["needs_rehash"] is False


def test_apply_repoints_tombstones_and_handles_collision():
    conn = _db(); gh = _seed_group(conn)
    mdc.apply_plan(conn, mdc.build_plan(conn), dry_run=False)
    # survivor keeps its own row; loser evidence re-pointed except the collision (deleted)
    ev = {r["key_finding"] for r in conn.execute(
        "SELECT key_finding FROM claim_evidence WHERE claim_id=1")}
    assert ev == {"s-a", "l-b", "l-c"}          # l-collide deleted, l-b/l-c re-pointed
    assert conn.execute("SELECT COUNT(*) FROM claim_evidence WHERE claim_id IN (2,3)").fetchone()[0] == 0
    # worker_result_id preserved on re-points (orphan sweep never fires)
    assert conn.execute("SELECT worker_result_id FROM claim_evidence WHERE key_finding='l-b'").fetchone()[0] == 201
    assert conn.execute("SELECT claim_id FROM adversarial_replications").fetchone()[0] == 1
    assert conn.execute("SELECT meta_claim_id FROM meta_transfer_predictions").fetchone()[0] == 1
    # losers tombstoned, survivor + unrelated untouched
    assert conn.execute("SELECT claim_status FROM knowledge_claims WHERE id=2").fetchone()[0] == "MERGED"
    assert conn.execute("SELECT merged_into FROM knowledge_claims WHERE id=3").fetchone()[0] == 1
    assert conn.execute("SELECT claim_status FROM knowledge_claims WHERE id=1").fetchone()[0] == "REPLICATED"
    assert conn.execute("SELECT claim_status FROM knowledge_claims WHERE id=9").fetchone()[0] == "ESTABLISHED"


def test_rollback_restores_everything():
    conn = _db(); gh = _seed_group(conn)
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("claim_evidence", "adversarial_replications", "meta_transfer_predictions")}
    stats = mdc.apply_plan(conn, mdc.build_plan(conn), dry_run=False)
    mdc.rollback(conn, stats["migration_ts"])
    for t, n in before.items():
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == n, t
    assert conn.execute("SELECT claim_status FROM knowledge_claims WHERE id=2").fetchone()[0] == "ESTABLISHED"
    assert conn.execute("SELECT claim_id FROM claim_evidence WHERE key_finding='l-b'").fetchone()[0] == 2
    assert conn.execute("SELECT claim_id FROM claim_evidence WHERE key_finding='l-collide'").fetchone()[0] == 2


def test_rehash_carries_hash_when_no_canonical_member():
    conn = _db()
    hyp = "orphan question with no canonical member"
    gh = mdc._norm_hash(hyp)
    # trailing routing-stats are stripped by normalize_hypothesis, so both
    # normalize to the same hash while neither stored claim_hash owns it
    conn.execute("INSERT INTO knowledge_claims VALUES (10,'staleA',?,?,?,?)",
                 (hyp + " (SR=0.1, N=3, routing=a)", "CANDIDATE", "ACTIVE", "9"))
    conn.execute("INSERT INTO knowledge_claims VALUES (11,'staleB',?,?,?,?)",
                 (hyp + " (SR=0.9, N=8, routing=b)", "REPLICATED", "ACTIVE", "8"))
    conn.commit()
    assert mdc._norm_hash(hyp + " (SR=0.1, N=3, routing=a)") == gh  # seed sanity
    plan = mdc.build_plan(conn)
    assert plan[gh]["needs_rehash"] is True
    mdc.apply_plan(conn, plan, dry_run=False)
    # survivor (higher tier = 11) carries the group hash forward
    assert conn.execute("SELECT claim_hash FROM knowledge_claims WHERE id=11").fetchone()[0] == gh


def test_merged_is_exempt_from_recompute():
    assert "MERGED" in maturity.EXEMPT_STATUSES
