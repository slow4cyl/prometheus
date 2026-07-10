"""Regression coverage for the historical lossy-domain backfill."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import backfill_lossy_domain_merges as backfill


class LossyDomainBackfillTests(unittest.TestCase):
    def test_canonical_targets_exclude_auto_generated_labels(self):
        self.assertIn("calibration", backfill.canonical_target_domains())
        self.assertNotIn("credit_card_fraud", backfill.canonical_target_domains())

    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE experiments (
                id TEXT PRIMARY KEY,
                domain TEXT,
                hypothesis TEXT,
                result TEXT
            );
            CREATE TABLE worker_results (
                id TEXT PRIMARY KEY,
                experiment_id TEXT,
                domain TEXT,
                finding TEXT
            );
            """
        )
        return conn

    def test_collect_candidates_covers_both_historical_writer_tables(self):
        conn = self._conn()
        conn.execute(
            "INSERT INTO experiments VALUES ('exp-1', 'calibration', 'transfer mechanism', 'result')"
        )
        conn.execute(
            "INSERT INTO worker_results VALUES ('wr-1', 'exp-1', 'safety', 'worker finding')"
        )

        rows = backfill.collect_candidates(conn, {"calibration", "safety"})

        self.assertEqual(
            {(row["table_name"], row["row_id"]) for row in rows},
            {("experiments", "exp-1"), ("worker_results", "wr-1")},
        )
        worker = next(row for row in rows if row["table_name"] == "worker_results")
        self.assertIn("transfer mechanism", worker["text"])
        self.assertIn("worker finding", worker["text"])

    def test_collect_candidates_preserves_result_context_with_a_long_hypothesis(self):
        conn = self._conn()
        conn.execute(
            "INSERT INTO experiments VALUES ('exp-2', 'calibration', ?, 'distinct result context')",
            ("h" * 2000,),
        )

        row = backfill.collect_candidates(conn, {"calibration"})[0]

        self.assertIn("distinct result context", row["text"])
        self.assertLessEqual(len(row["text"]), 601)

    def test_collect_candidates_preserves_worker_finding_with_a_long_parent_hypothesis(self):
        conn = self._conn()
        conn.execute("INSERT INTO experiments VALUES ('exp-3', 'calibration', ?, 'r')", ("h" * 2000,))
        conn.execute(
            "INSERT INTO worker_results VALUES ('wr-3', 'exp-3', 'safety', 'distinct worker finding')"
        )

        rows = backfill.collect_candidates(conn, {"safety"})
        row = rows[0]

        self.assertIn("distinct worker finding", row["text"])
        self.assertLessEqual(len(row["text"]), 601)

    def test_plan_moves_requires_confidence_and_margin(self):
        rows = [
            {"table_name": "experiments", "row_id": "move", "domain": "calibration", "text": "a"},
            {"table_name": "experiments", "row_id": "weak", "domain": "calibration", "text": "b"},
            {"table_name": "worker_results", "row_id": "same", "domain": "safety", "text": "c"},
        ]
        similarities = [
            {"physics": 0.75, "calibration": 0.40},
            {"physics": 0.49, "calibration": 0.10},
            {"safety": 0.80, "physics": 0.79},
        ]

        moves = backfill.plan_moves(rows, similarities, {"physics", "calibration", "safety"})

        self.assertEqual(moves, [("experiments", "move", "calibration", "physics", 0.75)])

    def test_plan_does_not_fall_back_from_a_noncanonical_top_match(self):
        rows = [{"table_name": "experiments", "row_id": "row", "domain": "calibration", "text": "x"}]
        similarities = [{"auto_generated_label": 0.90, "physics": 0.75, "calibration": 0.40}]

        moves = backfill.plan_moves(rows, similarities, {"physics", "calibration"})

        self.assertEqual(moves, [])

    def test_apply_and_rollback_preserve_table_identity_and_do_not_clobber_later_changes(self):
        conn = self._conn()
        conn.execute("INSERT INTO experiments VALUES ('shared', 'calibration', 'h', 'r')")
        conn.execute("INSERT INTO worker_results VALUES ('shared', 'shared', 'safety', 'f')")
        moves = [
            ("experiments", "shared", "calibration", "physics", 0.75),
            ("worker_results", "shared", "safety", "ml_security", 0.80),
        ]

        backfill.apply_moves(conn, moves, timestamp=123.0)
        self.assertEqual(conn.execute("SELECT domain FROM experiments WHERE id='shared'").fetchone()[0], "physics")
        self.assertEqual(conn.execute("SELECT domain FROM worker_results WHERE id='shared'").fetchone()[0], "ml_security")

        # A later classifier correction wins; rollback must not overwrite it.
        conn.execute("UPDATE worker_results SET domain='security' WHERE id='shared'")
        restored, skipped = backfill.rollback_moves(conn)

        self.assertEqual(restored, 1)
        self.assertEqual(skipped, 1)
        self.assertEqual(conn.execute("SELECT domain FROM experiments WHERE id='shared'").fetchone()[0], "calibration")
        self.assertEqual(conn.execute("SELECT domain FROM worker_results WHERE id='shared'").fetchone()[0], "security")


if __name__ == "__main__":
    unittest.main()
