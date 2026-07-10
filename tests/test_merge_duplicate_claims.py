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
    plan, _singles = mdc.build_plan(conn)
    assert set(plan) == {gh}
    # claim 1 owns the canonical hash -> survivor even though 2 is higher tier
    assert plan[gh]["survivor"]["id"] == 1
    assert sorted(plan[gh]["loser_ids"]) == [2, 3]
    assert plan[gh]["needs_rehash"] is False


def test_apply_repoints_tombstones_and_handles_collision():
    conn = _db(); gh = _seed_group(conn)
    mdc.apply_plan(conn, mdc.build_plan(conn)[0], dry_run=False)
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
    stats = mdc.apply_plan(conn, mdc.build_plan(conn)[0], dry_run=False)
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
    plan, _singles = mdc.build_plan(conn)
    assert plan[gh]["needs_rehash"] is True
    mdc.apply_plan(conn, plan, dry_run=False)
    # survivor (higher tier = 11) carries the group hash forward
    assert conn.execute("SELECT claim_hash FROM knowledge_claims WHERE id=11").fetchone()[0] == gh


def test_merged_is_exempt_from_recompute():
    assert "MERGED" in maturity.EXEMPT_STATUSES


def test_inline_dispute_markers_route_merged_to_survivor():
    """A MERGED tombstone is never re-statused, but a disagreed replication on
    its experiment routes through merged_into and disputes the SURVIVOR (the
    replication basis is experiment-keyed, so the merge's claim re-points
    can't cover it). The worker-contradiction marker skips MERGED entirely —
    its evidence already reached the survivor via the claim_evidence
    re-point."""
    import contradiction_detector as cd
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE knowledge_claims (id INTEGER PRIMARY KEY, hypothesis_text TEXT,
            claim_status TEXT, first_experiment_id TEXT, last_experiment_id TEXT,
            merged_into INTEGER, contradiction_count INTEGER DEFAULT 0,
            last_updated_at REAL);
        CREATE TABLE replication_results (original_experiment_id TEXT,
            validation_experiment_id TEXT, original_finding TEXT,
            validation_finding TEXT, replication_status TEXT);
        CREATE TABLE worker_results (experiment_id TEXT, hypothesis_supported INTEGER);
    """)
    conn.execute("INSERT INTO knowledge_claims (id, hypothesis_text, claim_status, first_experiment_id, merged_into) "
                 "VALUES (1,'q (10% success)','MERGED','exp_a',2)")
    conn.execute("INSERT INTO knowledge_claims (id, hypothesis_text, claim_status, first_experiment_id) "
                 "VALUES (2,'q','CANDIDATE','exp_s')")
    conn.execute("INSERT INTO replication_results VALUES ('exp_a','exp_b','f1','f2','disagreed')")
    conn.executemany("INSERT INTO worker_results VALUES ('exp_a', ?)", [(1,), (0,)])
    conn.commit()
    assert cd.detect_replication_contradictions(conn, dry_run=False) == 1
    assert conn.execute("SELECT claim_status FROM knowledge_claims WHERE id=2").fetchone()[0] == "DISPUTED"
    assert conn.execute("SELECT claim_status FROM knowledge_claims WHERE id=1").fetchone()[0] == "MERGED"
    assert cd.detect_worker_contradictions(conn, dry_run=True) == 0  # MERGED skipped


def test_non_merged_replication_dispute_unchanged():
    """The ordinary path — a live claim with a disagreed replication — still
    disputes that claim directly (merged_into NULL resolves to itself)."""
    import contradiction_detector as cd
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE knowledge_claims (id INTEGER PRIMARY KEY, hypothesis_text TEXT,
            claim_status TEXT, first_experiment_id TEXT, last_experiment_id TEXT,
            merged_into INTEGER, contradiction_count INTEGER DEFAULT 0,
            last_updated_at REAL);
        CREATE TABLE replication_results (original_experiment_id TEXT,
            validation_experiment_id TEXT, original_finding TEXT,
            validation_finding TEXT, replication_status TEXT);
    """)
    conn.execute("INSERT INTO knowledge_claims (id, hypothesis_text, claim_status, first_experiment_id) "
                 "VALUES (3,'live q','CANDIDATE','exp_c')")
    conn.execute("INSERT INTO replication_results VALUES ('exp_c','exp_d','f1','f2','disagreed')")
    conn.commit()
    assert cd.detect_replication_contradictions(conn, dry_run=False) == 1
    assert conn.execute("SELECT claim_status FROM knowledge_claims WHERE id=3").fetchone()[0] == "DISPUTED"


def test_stale_singleton_is_rehashed_and_rollbackable():
    """A lone claim carrying a stale hash gets carried to its canonical hash
    (so the next retest of its question attaches instead of forking); a
    canonical hash owned by any other claim is never taken; rollback
    restores the stale hash."""
    conn = _db()
    hyp = "a singleton question with a stale hash"
    gh = mdc._norm_hash(hyp)
    conn.execute("INSERT INTO knowledge_claims VALUES (20,'stale20',?,?,?,?)",
                 (hyp, "CANDIDATE", "ACTIVE", "7"))
    # unrelated claim OWNS some other canonical hash; also seed a claim that
    # already owns ITS canonical hash (must not appear in singles)
    hyp2 = "question already canonical"
    conn.execute("INSERT INTO knowledge_claims VALUES (21,?,?,?,?,?)",
                 (mdc._norm_hash(hyp2), hyp2, "CANDIDATE", "ACTIVE", "6"))
    conn.commit()
    plan, singles = mdc.build_plan(conn)
    assert (20, "stale20", gh) in singles
    assert all(cid != 21 for cid, _, _ in singles)
    stats = mdc.apply_plan(conn, plan, dry_run=False)
    stats.update(mdc.rehash_stale_singletons(conn, singles, ts=stats["migration_ts"], dry_run=False))
    assert stats.get("rehash_singleton") == 1
    assert conn.execute("SELECT claim_hash FROM knowledge_claims WHERE id=20").fetchone()[0] == gh
    mdc.rollback(conn, stats["migration_ts"])
    assert conn.execute("SELECT claim_hash FROM knowledge_claims WHERE id=20").fetchone()[0] == "stale20"


def test_stale_singleton_skipped_when_canonical_owned():
    """If another claim owns the canonical hash, the pair is a GROUP for the
    merge phase — the singleton pass must not touch it."""
    conn = _db()
    hyp = "owned canonical question"
    gh = mdc._norm_hash(hyp)
    conn.execute("INSERT INTO knowledge_claims VALUES (30,'stale30',?,?,?,?)",
                 (hyp + " (SR=0.5, N=2, routing=z)", "CANDIDATE", "ACTIVE", "5"))
    conn.execute("INSERT INTO knowledge_claims VALUES (31,?,?,?,?,?)",
                 (gh, hyp, "CANDIDATE", "ACTIVE", "4"))
    conn.commit()
    plan, singles = mdc.build_plan(conn)
    assert gh in plan  # it is a merge group
    assert all(canon != gh for _, _, canon in singles)
