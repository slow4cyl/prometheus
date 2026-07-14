CREATE TABLE system (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at REAL NOT NULL
    );
CREATE TABLE cycles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at REAL NOT NULL,
        completed_at REAL,
        status TEXT DEFAULT 'running',  -- running, completed, failed
        phase TEXT,
        summary TEXT,
        experiments_discovered INTEGER DEFAULT 0,
        experiments_synthesized INTEGER DEFAULT 0
    );
-- (sqlite_sequence is SQLite-internal; created automatically by AUTOINCREMENT)
CREATE TABLE experiments (
        id TEXT PRIMARY KEY,  -- exp_001, exp_002, etc.
        cycle_id INTEGER REFERENCES cycles(id),
        hypothesis TEXT,
        result TEXT,
        status TEXT DEFAULT 'pending',  -- pending, running, completed, failed
        confidence_change REAL DEFAULT 0,
        tags TEXT,  -- JSON array: ["CONFIRMED", "BREAKTHROUGH"]
        domain TEXT,
        model TEXT,
        created_at REAL NOT NULL,
        started_at REAL,
        completed_at REAL,
        workspace_path TEXT,
        kanban_task_id TEXT
    , refutation_type TEXT DEFAULT 'UNCERTAIN', predicted_direction TEXT DEFAULT NULL, observed_direction TEXT DEFAULT NULL, design_vector TEXT DEFAULT NULL, quality_score INTEGER DEFAULT NULL, quality_tags TEXT DEFAULT NULL, metrics_json TEXT, experiment_type TEXT DEFAULT NULL, mechanism_type TEXT DEFAULT NULL, benchmark_id TEXT DEFAULT NULL, verdict_basis TEXT);
CREATE TABLE domains (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        confidence REAL DEFAULT 0.5,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
CREATE TABLE subtopics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        domain_id INTEGER REFERENCES domains(id),
        topic TEXT NOT NULL,
        source_experiment TEXT,
        created_at REAL NOT NULL
    );
CREATE TABLE gaps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        domain_id INTEGER REFERENCES domains(id),
        description TEXT NOT NULL,
        status TEXT DEFAULT 'open',  -- open, closed
        closed_by_experiment TEXT,
        created_at REAL NOT NULL,
        closed_at REAL
    );
CREATE TABLE curiosities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        priority INTEGER DEFAULT 5,
        status TEXT DEFAULT 'active',  -- active, resolved, abandoned
        source_experiment TEXT,
        resolved_by_experiment TEXT,
        created_at REAL NOT NULL,
        resolved_at REAL
    , score REAL DEFAULT NULL, score_updated_at REAL DEFAULT NULL, repair_status TEXT DEFAULT NULL, template_type TEXT DEFAULT NULL, completeness_score INTEGER DEFAULT NULL, p_confirm REAL DEFAULT NULL, p_novel REAL DEFAULT NULL, p_expand REAL DEFAULT NULL, combined_score INTEGER DEFAULT NULL, source_result_id INTEGER, p_break REAL DEFAULT NULL, parent_curiosity_id INTEGER, evidence_depth INTEGER DEFAULT 0, direct_children INTEGER DEFAULT 0, total_descendants INTEGER DEFAULT 0, active_descendants INTEGER DEFAULT 0, benchmark_id TEXT DEFAULT NULL, known_answer TEXT DEFAULT NULL, provenance TEXT, generation_depth INTEGER DEFAULT 0);
CREATE TABLE skills (
        name TEXT PRIMARY KEY,
        category TEXT,
        created_at REAL NOT NULL,
        patched_at REAL,
        version TEXT
    );
CREATE TABLE self_mods (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_id INTEGER REFERENCES cycles(id),
        type TEXT,  -- skill, config, memory, prompt
        description TEXT,
        created_at REAL NOT NULL
    );
CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        cycle_id INTEGER REFERENCES cycles(id),
        entry_type TEXT,  -- DIRECTOR_PASS, CYCLE, JANITOR, SYNTHESIS
        content TEXT NOT NULL
    );
CREATE TABLE metrics_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        cycles_completed INTEGER,
        experiments_run INTEGER,
        experiments_completed INTEGER,
        knowledge_gaps_closed INTEGER,
        skills_created INTEGER,
        self_modifications INTEGER,
        cost_total REAL,
        cache_hit_rate REAL
    );
CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        title TEXT,
        body TEXT,
        status TEXT,
        assignee TEXT,
        priority INTEGER DEFAULT 0,
        created_at REAL,
        started_at REAL,
        completed_at REAL,
        result TEXT,
        workspace_path TEXT
    );
CREATE TABLE task_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT REFERENCES tasks(id),
        kind TEXT,
        payload TEXT,
        created_at REAL NOT NULL
    );
CREATE TABLE capabilities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        description TEXT NOT NULL,
        source_experiment TEXT,
        created_at REAL NOT NULL
    );
CREATE TABLE constraints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        description TEXT NOT NULL,
        created_at REAL NOT NULL
    );
CREATE TABLE goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        description TEXT NOT NULL,
        status TEXT DEFAULT 'active',
        created_at REAL NOT NULL
    );
CREATE TABLE from_isaac (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message TEXT NOT NULL,
        created_at REAL NOT NULL
    );
CREATE TABLE heartbeats (
    task_id TEXT PRIMARY KEY,
    worker_id TEXT,
    timestamp REAL NOT NULL,
    status TEXT DEFAULT 'alive',
    details TEXT
);
CREATE TABLE "worker_results" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    kanban_task_id TEXT,
    hypothesis_supported INTEGER,  -- 1=supported, 0=refuted, NULL=unknown
    key_finding TEXT,
    confidence REAL,
    domain TEXT,
    tags TEXT,  -- JSON array
    files_produced TEXT,  -- JSON array of file paths
    queue_additions TEXT,  -- JSON array of new queue items to add
    worker_id TEXT,
    created_at INTEGER DEFAULT (CAST(strftime('%s', 'now') AS INTEGER)),
    applied INTEGER DEFAULT 0  -- 1=synced to experiments table + self_state.json
-- finding_hash is a RETIRED legacy column: workers from the pre-defork
-- 50-profile fleet wrote their own ad-hoc hashes into it (mixed sha256/md5/
-- truncated formats, 1,777 rows, last write 2026-07-12); no script reads or
-- writes it. Kept for column-order stability on the live DB; do not revive.
, predicted_direction TEXT, observed_direction TEXT, design_vector TEXT, finding_hash TEXT, state_vector TEXT, experiment_type TEXT, mechanism_type TEXT DEFAULT NULL, bridge_attempts INTEGER DEFAULT 0, calibrated_confidence REAL, model TEXT, finding TEXT, files TEXT, queue TEXT, supported INTEGER, artifact_status TEXT DEFAULT 'UNVERIFIED', benchmark_id TEXT DEFAULT NULL, experiment_completed_at REAL, verdict_basis TEXT);
CREATE TABLE transfer_tracking (
    id INTEGER PRIMARY KEY,
    source_result_id INTEGER NOT NULL,
    source_domain TEXT NOT NULL,
    target_domain TEXT NOT NULL,
    task_id TEXT,
    destination_result_id INTEGER,
    destination_domain TEXT,
    status TEXT DEFAULT 'queued',
    created_at REAL DEFAULT (strftime('%s','now')),
    task_created_at REAL,
    task_completed_at REAL
, transfer_source TEXT DEFAULT 'explicit_transfer');
CREATE TABLE synthesis_versions (
            version_id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot TEXT NOT NULL,
            created_at TEXT NOT NULL,
            reason TEXT DEFAULT 'update',
            triggering_finding_id TEXT,
            state_vector TEXT
        );
CREATE TABLE synthesis_conflicts (
            conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
            finding_a_id TEXT NOT NULL,
            finding_b_id TEXT NOT NULL,
            conflict_type TEXT NOT NULL,
            description TEXT,
            resolved INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );
CREATE TABLE code_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            action TEXT NOT NULL,
            reason TEXT,
            old_hash TEXT,
            new_hash TEXT,
            diff_summary TEXT,
            created_by TEXT,
            created_at REAL NOT NULL
        );
CREATE TABLE topology_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    node_count INTEGER,
    edge_count INTEGER,
    total_routing_traffic INTEGER,
    routing_entropy REAL,
    top1_domain TEXT,
    top1_share REAL,
    top3_share REAL,
    top10_share REAL,
    pagerank_gini REAL,
    routing_gini REAL,
    mean_centroid_distance REAL,
    min_centroid_distance REAL,
    centroid_variance REAL,
    community_count INTEGER,
    edge_novelty_rate REAL,
    clustering_coefficient REAL
, experiments_completed INTEGER DEFAULT 0, worker_results_written INTEGER DEFAULT 0, transfers_added INTEGER DEFAULT 0, code_changes_count INTEGER DEFAULT 0, synthesis_outputs_added INTEGER DEFAULT 0);
CREATE TABLE topology_domain_snapshots (
    snapshot_id INTEGER NOT NULL,
    domain TEXT NOT NULL,
    pagerank REAL,
    betweenness REAL,
    experiment_count INTEGER,
    routing_degree INTEGER,
    community_id TEXT,
    centroid_norm REAL,
    PRIMARY KEY (snapshot_id, domain),
    FOREIGN KEY (snapshot_id) REFERENCES topology_snapshots(snapshot_id)
);
CREATE TABLE topology_alerts (
    alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    timestamp REAL NOT NULL,
    alert_type TEXT NOT NULL,
    severity TEXT DEFAULT 'info',
    message TEXT,
    metrics_json TEXT,
    FOREIGN KEY (snapshot_id) REFERENCES topology_snapshots(snapshot_id)
);
CREATE TABLE domain_redirects (
    old_domain TEXT NOT NULL,
    new_domain TEXT NOT NULL,
    reason TEXT,
    created_at REAL DEFAULT (strftime('%s','now')),
    UNIQUE(old_domain)
);
CREATE TABLE domain_promotions (
    domain TEXT PRIMARY KEY,
    status TEXT DEFAULT 'provisional',  -- provisional | candidate | canonical
    first_seen REAL,
    experiment_count INTEGER DEFAULT 0,
    last_growth_at REAL,
    promotion_eligible_at REAL,
    promoted_at REAL,
    notes TEXT
);
CREATE TABLE domain_redirect_reversals (
    redirect_id INTEGER,
    reversed_at REAL,
    reason TEXT,
    FOREIGN KEY (redirect_id) REFERENCES domain_redirects(rowid)
);
CREATE TABLE architectural_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_text TEXT NOT NULL,
            claim_type TEXT NOT NULL CHECK(claim_type IN ('mechanism', 'record', 'epistemic', 'governance')),
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'downgraded', 'retired', 'uncertain')),
            confidence REAL DEFAULT 0.5,
            evidence TEXT,
            mechanism TEXT,
            behavioral_consequence TEXT,
            counterfactual TEXT,
            created_at REAL NOT NULL,
            last_audit_at REAL,
            audit_count INTEGER DEFAULT 0
        , preconditions TEXT, prior_work_citation TEXT, prior_work_status TEXT DEFAULT '', last_adversarial_audit_at REAL, falsifiability_check TEXT, trivial_match_check TEXT, limit_claim_check TEXT, audit_notes TEXT, benchmark_id TEXT DEFAULT NULL);
CREATE TABLE claim_audit_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL,
            verdict TEXT NOT NULL CHECK(verdict IN ('confirmed', 'refined', 'downgraded', 'retired', 'insufficient_evidence')),
            reason TEXT NOT NULL,
            confidence_before REAL,
            confidence_after REAL,
            audited_at REAL NOT NULL, preconditions TEXT, falsifiability_check TEXT, trivial_match_check TEXT, limit_claim_check TEXT, auditor_notes TEXT,
            FOREIGN KEY (claim_id) REFERENCES architectural_claims(id)
        );
CREATE TABLE replication_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    original_experiment_id TEXT NOT NULL,
    original_finding TEXT,
    original_confidence REAL,
    original_domain TEXT,
    validation_task_id TEXT,
    validation_experiment_id TEXT,
    validation_finding TEXT,
    validation_confidence REAL,
    validation_hypothesis_supported INTEGER,
    replication_status TEXT DEFAULT 'pending',
    selected_at INTEGER,
    validated_at INTEGER,
    selection_reason TEXT, break_informativeness REAL DEFAULT 0, overlap_coefficient REAL DEFAULT 0, analytic_verdict TEXT DEFAULT '',
    UNIQUE(original_experiment_id)
);
CREATE TABLE knowledge_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_hash TEXT UNIQUE NOT NULL,
    hypothesis_text TEXT NOT NULL,
    normalized_text TEXT,
    claim_summary TEXT,
    domain TEXT,
    posterior REAL DEFAULT 0.5,
    support_count INTEGER DEFAULT 0,
    refute_count INTEGER DEFAULT 0,
    boundary_count INTEGER DEFAULT 0,
    uncertain_count INTEGER DEFAULT 0,
    total_evidence INTEGER DEFAULT 0,
    status TEXT DEFAULT 'UNTESTED',
    created_at TEXT,
    last_updated_at TEXT,
    first_experiment_id TEXT,
    last_experiment_id TEXT
, prior_work_status TEXT DEFAULT '', prior_work_citation TEXT DEFAULT '', claim_status TEXT DEFAULT 'HISTORICAL_UNKNOWN', contradiction_count INTEGER DEFAULT 0, artifact_status TEXT DEFAULT 'HISTORICAL_UNKNOWN', weighted_support_count REAL DEFAULT 0.0, claim_type TEXT DEFAULT 'DIRECTIONAL', spurious_agreement REAL, sa_flag_mismatch REAL, sa_direction_disagree REAL, sa_magnitude_spread REAL, sa_computed_at REAL, n_independent_retests INTEGER DEFAULT 0, transfer_survival_score REAL, n_formal_replications INTEGER DEFAULT 0, circular_construction INTEGER, is_meta INTEGER, is_empirical_fact INTEGER, method_code_mismatch INTEGER);
CREATE TABLE claim_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES knowledge_claims(id),
    experiment_id TEXT NOT NULL,
    worker_result_id INTEGER,
    evidence_type TEXT,
    confidence REAL,
    key_finding TEXT,
    domain TEXT,
    attached_at TEXT, is_cross_domain INTEGER DEFAULT 0,
    UNIQUE(claim_id, worker_result_id)
);
CREATE TABLE claim_skill_deps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL,
            skill_name TEXT,
            section TEXT,
            reference_file TEXT,
            task_body_fragment TEXT,
            dependency_type TEXT CHECK(dependency_type IN ('PROCEDURE', 'ASSUMPTION', 'THRESHOLD')),
            registered_at REAL,
            FOREIGN KEY (claim_id) REFERENCES architectural_claims(id)
        );
CREATE TABLE claim_config (
            key TEXT PRIMARY KEY,
            value REAL,
            description TEXT,
            updated_at REAL
        );
CREATE TABLE knowledge_claims_audit_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        claim_id INTEGER NOT NULL,
        audit_timestamp TEXT DEFAULT (datetime('now')),
        prior_work_citation TEXT,
        prior_work_status TEXT,
        verdict TEXT,
        reason TEXT,
        confidence_before REAL,
        confidence_after REAL,
        FOREIGN KEY (claim_id) REFERENCES knowledge_claims(id)
    );
CREATE TABLE compression_bottlenecks (
            id              INTEGER PRIMARY KEY,
            mechanism       TEXT NOT NULL,
            fan_out_domains INTEGER NOT NULL,   -- distinct domains it spans
            claim_count     INTEGER NOT NULL,   -- total claims under it
            domains_json    TEXT,               -- {domain: count}
            exception_json  TEXT,               -- domains where a DISPUTED claim exists
            unifying_q      TEXT,               -- the question injected (if any)
            injected        INTEGER DEFAULT 0,  -- 1 if fed back to queue
            created_at      REAL NOT NULL,
            run_id          TEXT
        , exemplar TEXT);
CREATE TABLE experiment_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT UNIQUE,
    title TEXT,
    source_domain TEXT,
    target_domain TEXT,
    experiment_type TEXT,
    mechanism_type TEXT,
    hypothesis TEXT,
    hypothesis_confirmed INTEGER,
    success_count INTEGER,
    total_count INTEGER,
    success_rate REAL,
    transfers_json TEXT,
    created_at TEXT,
    kanban_task_id TEXT
);
CREATE TABLE calibration_model_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, trained_at INTEGER, n_train INTEGER,
        challenger_auc REAL, challenger_brier REAL, challenger_resolution REAL,
        scalar_auc REAL, scalar_brier REAL,
        champion_auc REAL, champion_brier REAL,
        promoted INTEGER, reasons TEXT);
CREATE TABLE domain_malformed_log (
            malformed_domain TEXT NOT NULL,
            components TEXT NOT NULL,
            first_seen REAL DEFAULT (strftime('%s','now')),
            hit_count INTEGER DEFAULT 1,
            UNIQUE(malformed_domain)
        );
CREATE TABLE synthesis_outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    synthesis_task_id TEXT NOT NULL,
    worker_id TEXT DEFAULT 'prometheus-synthesis',
    experiments_covered TEXT,
    queue_resolutions TEXT,
    new_curiosities TEXT,
    counter_overrides TEXT,
    domain_confidence_updates TEXT,
    key_patterns TEXT,
    created_at INTEGER DEFAULT (CAST(strftime('%s', 'now') AS INTEGER)),
    applied INTEGER DEFAULT 0,
    provenance TEXT DEFAULT NULL,
    valid_until TEXT DEFAULT NULL,
    cross_pollination TEXT
);
CREATE TABLE syn_ids (exp_id TEXT);
CREATE TABLE _unsyn (eid TEXT);
CREATE UNIQUE INDEX idx_transfer_unique
ON transfer_tracking(source_result_id, target_domain);
CREATE INDEX idx_experiments_status ON experiments(status);
CREATE INDEX idx_experiments_domain ON experiments(domain);
CREATE INDEX idx_experiments_cycle ON experiments(cycle_id);
CREATE INDEX idx_cycles_status ON cycles(status);
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX idx_audit_type ON audit_log(entry_type);
CREATE INDEX idx_heartbeats_ts ON heartbeats(timestamp);
CREATE INDEX idx_heartbeats_status ON heartbeats(status);
CREATE INDEX idx_experiments_refutation ON experiments(refutation_type);
CREATE INDEX idx_transfer_source
ON transfer_tracking(source_result_id);
CREATE INDEX idx_transfer_task
ON transfer_tracking(task_id);
CREATE INDEX idx_transfer_status
ON transfer_tracking(status);
CREATE INDEX idx_code_changes_file ON code_changes(file_path);
CREATE INDEX idx_code_changes_created ON code_changes(created_at);
CREATE INDEX idx_ts_timestamp ON topology_snapshots(timestamp);
CREATE INDEX idx_tdss_snapshot ON topology_domain_snapshots(snapshot_id);
CREATE INDEX idx_ta_timestamp ON topology_alerts(timestamp);
CREATE INDEX idx_ta_type ON topology_alerts(alert_type);
CREATE INDEX idx_curiosities_score ON curiosities(score DESC);
CREATE INDEX idx_curiosities_status_score ON curiosities(status, score DESC);
CREATE INDEX idx_curiosities_source_result ON curiosities(source_result_id);
CREATE INDEX idx_repl_status ON replication_results(replication_status);
CREATE INDEX idx_repl_original ON replication_results(original_experiment_id);
CREATE INDEX idx_kc_status ON knowledge_claims(status);
CREATE INDEX idx_kc_hash ON knowledge_claims(claim_hash);
CREATE INDEX idx_kc_domain ON knowledge_claims(domain);
CREATE INDEX idx_kc_posterior ON knowledge_claims(posterior);
CREATE INDEX idx_ce_claim ON claim_evidence(claim_id);
CREATE INDEX idx_ce_experiment ON claim_evidence(experiment_id);
CREATE INDEX idx_curiosities_resolved ON curiosities(resolved_by_experiment);
CREATE UNIQUE INDEX idx_worker_results_experiment_id ON worker_results(experiment_id);
CREATE VIEW gate_health AS
SELECT 
    (SELECT COUNT(*) FROM domain_redirects) as total_redirects,
    (SELECT COUNT(*) FROM domain_redirect_reversals) as reversals,
    (SELECT COUNT(*) FROM domain_promotions WHERE status='provisional') as provisional,
    (SELECT COUNT(*) FROM domain_promotions WHERE status='candidate') as candidate,
    (SELECT COUNT(*) FROM domain_promotions WHERE status='canonical') as promoted,
    (SELECT COUNT(*) FROM (SELECT domain FROM experiments GROUP BY domain HAVING COUNT(*) >= 10)) as canonical_domains,
    (SELECT COUNT(*) FROM (SELECT domain FROM experiments GROUP BY domain HAVING COUNT(*) < 10)) as small_domains
/* gate_health(total_redirects,reversals,provisional,candidate,promoted,canonical_domains,small_domains) */;
CREATE UNIQUE INDEX ux_claim_evidence_dedup
ON claim_evidence(claim_id, key_finding);
CREATE TABLE claim_status_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_id INTEGER NOT NULL,
                changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                old_status TEXT,
                new_status TEXT NOT NULL,
                blocking_reason TEXT,
                wsc_at_change REAL,
                n_retests_at_change INTEGER,
                sa_at_change REAL,
                contradiction_count_at_change INTEGER
            );
CREATE INDEX idx_csh_claim_time ON claim_status_history(claim_id, changed_at);
CREATE INDEX idx_wr_exp ON worker_results(experiment_id);
CREATE INDEX idx_tt_status ON transfer_tracking(status);
CREATE INDEX idx_e_id ON experiments(id);
CREATE TABLE contested_transfer_pairs (
            source       TEXT NOT NULL,
            target       TEXT NOT NULL,
            n            INTEGER NOT NULL,
            pos_rate     REAL NOT NULL,
            coinflip     REAL NOT NULL,
            updated_at   REAL NOT NULL,
            PRIMARY KEY (source, target)
        );
CREATE INDEX idx_wr_kanban ON worker_results(kanban_task_id);
CREATE INDEX idx_so_exp ON synthesis_outputs(experiments_covered);
CREATE TRIGGER trg_experiments_require_id
BEFORE INSERT ON experiments
FOR EACH ROW
WHEN NEW.id IS NULL OR TRIM(NEW.id) = ''
BEGIN
    SELECT RAISE(ABORT, 'experiments.id must be a non-empty string');
END;
CREATE TABLE answer_adjudications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL,
            adjudicated_at REAL NOT NULL,
            evidence_fingerprint TEXT NOT NULL,
            n_supports INTEGER,
            consistent INTEGER,           -- 1 yes / 0 contradictory / NULL parse-or-API failure
            incompatibilities TEXT,       -- JSON [{a, b, reason}]
            summary TEXT,
            model TEXT
        );
CREATE INDEX idx_adj_claim_fp
        ON answer_adjudications(claim_id, evidence_fingerprint);
CREATE TABLE adversarial_replications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_id INTEGER NOT NULL,
                kanban_task_id TEXT,
                experiment_id TEXT,
                created_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',  -- pending|survived|refuted|expired
                resolved_at REAL,
                notes TEXT
            , attacker_model TEXT);
CREATE INDEX idx_advrep_claim
            ON adversarial_replications(claim_id, status);
CREATE TABLE circularity_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL,
            reviewed_at REAL NOT NULL,
            evidence_fingerprint TEXT NOT NULL,
            circular INTEGER,             -- 1 flagged / 0 clean / NULL failed
            confidence REAL,
            reason TEXT,
            code_seen INTEGER,            -- how many experiments had archived code
            model TEXT
        );
CREATE INDEX idx_circ_claim_fp
        ON circularity_reviews(claim_id, evidence_fingerprint);
CREATE TABLE dispute_arbitrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL,
            adjudication_id INTEGER,
            kanban_task_id TEXT,
            experiment_id TEXT,
            side_a TEXT,                  -- JSON list of experiment ids
            side_b TEXT,                  -- JSON list of experiment ids
            n_pairs INTEGER,
            created_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
              -- pending|resolved_a|resolved_b|both_wrong|regime_split|expired
            resolved_at REAL,
            notes TEXT
        , source_kind TEXT DEFAULT 'adjudication', rr_ids TEXT, ar_ids TEXT, triage TEXT, model_override TEXT);
CREATE INDEX idx_darb_claim
        ON dispute_arbitrations(claim_id, status);
CREATE TABLE novelty_audits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL,
            verdict TEXT NOT NULL,
            confidence REAL,
            citations TEXT,       -- JSON array of {ref, covers}
            explanation TEXT,
            novel_residue TEXT,   -- the part of the claim NOT found in prior work
            model TEXT,
            web_used INTEGER DEFAULT 1,
            created_at REAL NOT NULL
        , residue_injected INTEGER DEFAULT 0, corroborated INTEGER, finder_model TEXT, finder_found TEXT, search_adequacy REAL);
CREATE INDEX idx_novelty_claim ON novelty_audits(claim_id);
CREATE TABLE analytic_verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL,
            verdict TEXT NOT NULL,           -- VERIFIED | MISMATCH
            stated REAL, expr TEXT, value REAL, diff REAL, tol REAL,
            domain TEXT,
            source_finding TEXT,             -- the finding text the identity came from
            enqueued_recompute INTEGER DEFAULT 0,
            model TEXT DEFAULT 'symbolic_verifier',
            created_at REAL NOT NULL
        );
CREATE INDEX idx_av_claim ON analytic_verifications(claim_id);
CREATE TABLE meta_transfer_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meta_claim_id INTEGER NOT NULL,
            target_domain TEXT NOT NULL,
            predicted_direction INTEGER,      -- +1 predicted to generalize, -1 not
            kanban_task_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at REAL NOT NULL
        , observed_direction INTEGER, outcome TEXT, reconciled_at REAL);
CREATE INDEX idx_mtp_claim ON meta_transfer_predictions(meta_claim_id);
CREATE TABLE contradiction_attacks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL,
            novelty_audit_id INTEGER,
            kanban_task_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',   -- enqueued|enqueue_failed
            created_at REAL NOT NULL
        );
CREATE INDEX idx_ca_claim ON contradiction_attacks(claim_id);
CREATE TABLE claim_scopes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        claim_id INTEGER NOT NULL,
        scope_text TEXT NOT NULL,
        source_experiment TEXT,
        source_curiosity_id INTEGER,
        created_at REAL,
        UNIQUE(claim_id, source_curiosity_id));
CREATE INDEX idx_claim_scopes_claim ON claim_scopes(claim_id);
CREATE TABLE discovery_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL UNIQUE,
            discovery_score REAL NOT NULL,
            novelty_confidence REAL,
            tier TEXT,
            wsc REAL,
            survivals INTEGER,
            hardening_task_id TEXT,
            status TEXT NOT NULL DEFAULT 'scored',  -- scored|hardening|likely_search_miss|hardened|broken
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        , route TEXT, route_reason TEXT);
CREATE INDEX idx_disc_score ON discovery_candidates(discovery_score);
CREATE TABLE xdomain_disconfirm_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL UNIQUE,
            domain TEXT,
            n_cross_refute INTEGER,
            kanban_task_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',   -- enqueued|enqueue_failed
            created_at REAL NOT NULL
        );
CREATE INDEX idx_xddc_claim ON xdomain_disconfirm_checks(claim_id);
CREATE TABLE task_prior_feed (
            kanban_task_id TEXT PRIMARY KEY,
            prior_fed INTEGER NOT NULL,
            n_fed INTEGER DEFAULT 0,
            fed_hashes TEXT,
            created_at REAL NOT NULL);
CREATE TABLE claim_independence (
            claim_id INTEGER PRIMARY KEY,
            independence_ratio REAL, n_sup INTEGER, n_stamped INTEGER, n_stamped_fed INTEGER,
            families TEXT, single_family INTEGER, independence_multiplier REAL, updated_at REAL);
CREATE TABLE clean_room_replications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        claim_id INTEGER NOT NULL,
        kanban_task_id TEXT,
        experiment_id TEXT,
        model TEXT,
        created_at REAL NOT NULL);
CREATE TABLE world_groundings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        claim_id INTEGER NOT NULL,
        kanban_task_id TEXT,
        experiment_id TEXT,
        model TEXT,
        created_at REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',   -- pending|resolved|dead
        outcome TEXT,                             -- HOLDS|FAILS|NO_DATASET
        dataset TEXT,
        basis TEXT,                               -- world_basis() verdict
        verified INTEGER,                         -- 1 = outcome basis checks out
        resolved_at REAL);
CREATE TABLE recall_audits (
            id INTEGER PRIMARY KEY,
            claim_id INTEGER NOT NULL,
            audit_id INTEGER NOT NULL,
            layer TEXT NOT NULL,
            verdict TEXT NOT NULL,
            relationship TEXT,
            citation TEXT,
            reason TEXT,
            model TEXT,
            n_index_hits INTEGER,
            created_at REAL NOT NULL, suggested_citation TEXT);
CREATE INDEX ix_recall_audit_id ON recall_audits(audit_id);
CREATE TABLE claim_near_duplicates (
            claim_a INTEGER NOT NULL,
            claim_b INTEGER NOT NULL,
            similarity REAL NOT NULL,
            tier TEXT NOT NULL,
            status_a TEXT, status_b TEXT,
            detected_at REAL NOT NULL,
            PRIMARY KEY (claim_a, claim_b));
CREATE TABLE synthesis_curiosity_links (
            synthesis_output_id INTEGER NOT NULL,
            synthesis_task_id TEXT NOT NULL,
            curiosity_id INTEGER NOT NULL,
            method TEXT,
            created_at REAL NOT NULL,
            PRIMARY KEY (synthesis_output_id, curiosity_id)
        );
CREATE UNIQUE INDEX ux_synthesis_curiosity_links_curiosity ON synthesis_curiosity_links(curiosity_id);
CREATE INDEX idx_synthesis_curiosity_links_task ON synthesis_curiosity_links(synthesis_task_id);
CREATE TABLE method_code_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL,
            reviewed_at REAL NOT NULL,
            evidence_fingerprint TEXT NOT NULL,
            mismatch INTEGER,             -- 1 flagged / 0 aligned / NULL failed
            confidence REAL,
            reason TEXT,
            code_seen INTEGER,
            model TEXT
        );
CREATE INDEX idx_mca_claim_fp
        ON method_code_reviews(claim_id, evidence_fingerprint);
