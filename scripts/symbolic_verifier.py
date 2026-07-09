#!/usr/bin/env python3
"""symbolic_verifier.py — analytic verification lane for numeric claims.

Why (2026-07-04): analytic_verifier.py only regex-FLAGS limit claims as
'conjectured' ("Does NOT attempt symbolic verification (future enhancement)").
Meanwhile the two worst corrections of the epistemic-hardening pass were both
numeric-vs-closed-form errors (a Hopf threshold reported 1.11 that is actually
9*2**(1/3) = 11.339…; "linear" scaling that is actually 2**d). This lane does
the missing verification, scoped to what is honestly checkable WITHOUT an LLM:

  1. EQUALITY CHECK — a finding that states "<decimal> = <closed-form expr>"
     (e.g. "11.34 = 9*2^(1/3)", "threshold ≈ π/4") gets the expression
     evaluated (safe ast evaluator, no eval()) and compared at the precision
     the decimal was stated to.
       * match within rounding  → a row in the dedicated analytic_verifications
         ledger (verdict='VERIFIED'). POSTERIOR-NEUTRAL audit trail, NOT support:
         the identity is usually an incidental constant, orthogonal to the
         directional hypothesis (see record_verification). It goes in its own
         table — NOT claim_evidence — because contradiction_detector sweeps
         worker_result_id-NULL evidence rows as orphans every 15 min. This path
         is SAFE against spurious extraction: a decimal wrongly paired with an
         unrelated closed form cannot satisfy numeric equality, so it never
         verifies.
       * mismatch far beyond rounding → REPORT-ONLY by default. It is a *lead*,
         not an action: worker prose uses '=' for assignment everywhere and '≈'
         for loose/inverse notation ("reduction is 4/7 ≈ 1.75x"), both of which
         read as false mismatches. Only with --enqueue-mismatch does a mismatch
         enqueue a targeted recompute via adversarial_replication_enqueuer.py.
         Literature-rule analogue: the analytic check is a *detector*; only a
         computational recompute (attack lane) may demote a claim.
  2. CONSTANT PROVENANCE — salient decimals (>=3 decimal places) are matched
     against named constants (pi, e, Feigenbaum delta/alpha, Catalan, ln2,
     2**(1/3), small multiples…) and, when mpmath is importable (vllm-env
     python has it; system python3 does not), mpmath.identify(). Matches go to
     the run REPORT only — provenance is a lead for the novelty/attack lanes,
     not evidence by itself.

Evidence rows use synthetic experiment ids 'exp_ana_<claim>_<yymmddHHMM>',
mirroring the arbitration lane's 'exp_arb_…' convention, then recompute
posterior/status via claim_lifecycle's own functions and refresh the claim
summary. Timestamps: DB columns keep their epoch-float defaults
(CAST(strftime('%s','now') AS REAL)) — never ISO, never datetime('now').
hypothesis_text is never touched.

WAL discipline: no LLM calls; one short write txn per claim (commit per claim).

Usage:
    python3 symbolic_verifier.py                     # dry-run over the shelf
    python3 symbolic_verifier.py --claim 63838       # one claim, dry-run
    python3 symbolic_verifier.py --apply             # write ledger rows
    python3 symbolic_verifier.py --apply --enqueue-mismatch  # + recompute leads
    ~/vllm-env/bin/python symbolic_verifier.py --apply   # + mpmath.identify
"""
import argparse
import ast
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time

DB = os.path.expanduser('~/.hermes/prometheus.db')
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.expanduser('~/.hermes/artifacts/symbolic_verifier')
ENQUEUER = os.path.join(SCRIPTS, 'adversarial_replication_enqueuer.py')

try:
    import mpmath  # optional: present in ~/vllm-env, absent in system python3
    HAVE_MPMATH = True
except ImportError:
    HAVE_MPMATH = False

# ── named constants for provenance matching ─────────────────────────────────
NAMED = {
    'pi': math.pi, 'e': math.e, '2*pi': 2 * math.pi, 'pi/2': math.pi / 2,
    'pi/4': math.pi / 4, '1/pi': 1 / math.pi, '1/e': 1 / math.e,
    'pi**2/6': math.pi ** 2 / 6, 'sqrt(2*pi)': math.sqrt(2 * math.pi),
    'sqrt(2)': math.sqrt(2), 'sqrt(3)': math.sqrt(3), 'sqrt(5)': math.sqrt(5),
    'golden_ratio': (1 + math.sqrt(5)) / 2, 'ln(2)': math.log(2),
    'ln(10)': math.log(10), 'e**2': math.e ** 2,
    '2**(1/3)': 2 ** (1 / 3), '3**(1/3)': 3 ** (1 / 3),
    'feigenbaum_delta': 4.669201609102991, 'feigenbaum_alpha': 2.502907875095893,
    'catalan': 0.915965594177219, 'euler_gamma': 0.5772156649015329,
    'zeta(3)': 1.2020569031595943,
}
# small integer multiples of the most physically recurrent bases
for _k in range(2, 13):
    for _n in ('pi', 'e', 'sqrt(2)', 'sqrt(3)', '2**(1/3)', '3**(1/3)', 'ln(2)'):
        NAMED[f'{_k}*{_n}'] = _k * NAMED[_n]

# metric-context tokens whose numbers are measurements, not candidate constants
_METRIC_CTX = re.compile(
    r'(?:^|[\s(])(?:p|r|r2|r\^2|rho|tau|auc|cv|rmse|mae|mse|n|df|seed|alpha|beta'
    r'|lr|epoch|step|k|z)\s*[=≈:<>]\s*-?$', re.IGNORECASE)

# ── safe expression evaluation (ast whitelist — no eval, no sympy needed) ───
_FUNCS = {'sqrt': math.sqrt, 'cbrt': lambda x: math.copysign(abs(x) ** (1 / 3), x),
          'log': math.log, 'ln': math.log, 'log2': math.log2,
          'log10': math.log10, 'exp': math.exp, 'abs': abs,
          'sin': math.sin, 'cos': math.cos, 'tan': math.tan}
_NAMES = {'pi': math.pi, 'e': math.e, 'phi': (1 + math.sqrt(5)) / 2}
_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)


def _safe_eval(node):
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _safe_eval(node.operand)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
        a, b = _safe_eval(node.left), _safe_eval(node.right)
        if isinstance(node.op, ast.Pow):
            if abs(b) > 20 or (a == 0 and b < 0):
                raise ValueError('pow out of range')
            if a < 0 and b != int(b):
                raise ValueError('fractional power of negative base')
            return a ** b
        if isinstance(node.op, ast.Add):
            return a + b
        if isinstance(node.op, ast.Sub):
            return a - b
        if isinstance(node.op, ast.Mult):
            return a * b
        if b == 0:
            raise ValueError('division by zero')
        return a / b
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in _FUNCS and not node.keywords and len(node.args) == 1:
        return float(_FUNCS[node.func.id](_safe_eval(node.args[0])))
    if isinstance(node, ast.Name) and node.id in _NAMES:
        return _NAMES[node.id]
    raise ValueError(f'disallowed node {type(node).__name__}')


def safe_eval(expr):
    """Evaluate a numeric closed-form expression string, or raise ValueError."""
    if len(expr) > 80:
        raise ValueError('expression too long')
    v = _safe_eval(ast.parse(expr, mode='eval'))
    if not math.isfinite(v):
        raise ValueError('non-finite result')
    return v


def normalize_symbols(s):
    """Symbol-level rewrite only (spaces preserved): 9·2^(1/3) → 9*2**(1/3),
    √2 → sqrt(2), π → pi. Whitespace is kept so a math-token run terminates at
    the first prose word rather than gluing it onto the expression."""
    s = s.replace('·', '*').replace('×', '*').replace('−', '-').replace('π', 'pi')
    s = re.sub(r'√\s*\(', 'sqrt(', s)
    s = re.sub(r'√\s*(\d+(?:\.\d+)?)', r'sqrt(\1)', s)
    s = s.replace('^', '**')
    return s


# a maximal run of MATH tokens only — numbers, operators, parens, and
# word-boundaried function/constant names. Prose words are not tokens, so the
# run ends at them (this is what stops "9*2**(1/3) not 1.11" swallowing "not…").
_NAME_TOK = r'sqrt|cbrt|log2|log10|log|ln|exp|sin|cos|tan|pi|phi|e'
_MATH_RUN = re.compile(
    r'(?:\d+\.?\d*|\*\*|[-+*/()]|(?:' + _NAME_TOK + r')\b|\s)+')
# a NON-TRIVIAL closed form: a binary operation whose LEFT side is a real
# operand (a number, a ')', or a named constant — NOT a leading unary sign),
# or a function call. This is the difference between a formula and a stray
# number near an '=' in worker prose:
#   "9*2**(1/3)", "pi/4", "1/e", "sqrt(2)"  → structure   (identity worth checking)
#   "(0.004)", "-5", "3.0", "pi"            → NO structure (bare value / fragment)
_OPERAND_L = r'(?:\d|\)|\b(?:pi|phi|e))'
_OPERAND_R = r'(?:\(|\d|sqrt|cbrt|log|ln|exp|\b(?:pi|phi|e)\b)'
_HAS_STRUCTURE = re.compile(
    r'(?:sqrt|cbrt|log2|log10|log|ln|exp|sin|cos|tan)\s*\('    # function call
    r'|' + _OPERAND_L + r'\s*(?:\*\*|[*/+\-])\s*' + _OPERAND_R)  # operand OP operand
_HAS_VALUE = re.compile(r'(\d|\bpi\b|\bphi\b|(?<![a-z])e(?![a-z]))')
_DEC = r'-?\d+\.\d{2,}'
_TRAIL_DEC = re.compile(_DEC + r'\s*$')
# a decimal immediately followed by a glued 'x'/'×' is a MULTIPLIER in prose
# ("4/7 ≈ 1.75x speedup"), not an equality operand — the '≈' there is inverse/loose
# notation, not a closed-form identity. Reject that side so it never pairs into a
# spurious MISMATCH. A SPACED 'x' ("1.75 x 2") is left alone (that is multiplication).
_LEAD_DEC = re.compile(r'\s*(' + _DEC + r')(?![xX×])')


def _try_expr(raw):
    """Return (normalized, value) for a self-contained closed form, or None.

    Rejects, in order: empty; a run that STARTS with a binary operator (that is
    a fragment torn from a larger expression whose head was a variable name —
    'alpha**2' → '**2', '4W/pi' → '/pi'); a run with no real operator/function
    (bare number or bare constant); and anything that doesn't evaluate."""
    norm = re.sub(r'\s+', '', raw)
    if not norm or norm[0] in '*/':          # leading binary op ⇒ clipped fragment
        return None
    if norm[:1] == '+':                       # leading '+' is only ever a fragment here
        return None
    if not _HAS_STRUCTURE.search(norm) or not _HAS_VALUE.search(norm):
        return None                           # pure number or bare name — nothing to verify
    try:
        return norm, safe_eval(norm)
    except (ValueError, SyntaxError, OverflowError, ZeroDivisionError,
            RecursionError, TypeError):
        return None


def _trailing_math(s):
    """Longest math run ending exactly where s (rstripped) ends — i.e. the
    expression immediately left-adjacent to an '=' sign."""
    runs = list(_MATH_RUN.finditer(s))
    if runs and runs[-1].end() == len(s):
        return _try_expr(runs[-1].group())
    return None


def _leading_math(s):
    """Longest math run starting at the front of s (after leading space)."""
    m = _MATH_RUN.match(s.lstrip())
    return _try_expr(m.group()) if m else None


def extract_equalities(text):
    """Yield (stated_float, decimals, expr_str, expr_val) for every checkable
    '<decimal> =/≈ <closed form>' statement (either side order). Symbols are
    normalized first; each '=' / '≈' is examined with a bounded left/right
    window so prose cannot leak into the expression."""
    t = normalize_symbols(text)
    seen = set()
    for m in re.finditer(r'[=≈]', t):
        i = m.start()
        left, right = t[max(0, i - 60):i], t[i + 1:i + 61]
        ld = _TRAIL_DEC.search(left)          # decimal immediately left of '='
        rd = _LEAD_DEC.match(right)           # decimal immediately right of '='
        lm = _trailing_math(left)             # expr immediately left of '='
        rm = _leading_math(right)             # expr immediately right of '='
        pairs = []
        if ld and rm:                         # <decimal> = <expr>
            pairs.append((ld.group().strip(), rm))
        if rd and lm:                         # <expr> = <decimal>
            pairs.append((rd.group(1), lm))
        for stated_s, (expr, val) in pairs:
            key = (stated_s, expr)
            if key in seen:
                continue
            seen.add(key)
            yield float(stated_s), _decimals(stated_s), expr, val


def _decimals(s):
    return len(s.split('.')[1]) if '.' in s else 0


def classify(stated, decimals, val):
    """rounding-aware verdict: VERIFIED within stated precision, MISMATCH when
    far beyond any rounding of the stated figure, else AMBIGUOUS."""
    tol = 0.51 * 10 ** (-decimals)
    diff = abs(stated - val)
    if diff <= tol:
        return 'VERIFIED', diff, tol
    if diff > max(50 * tol, 0.01 * abs(val)):
        return 'MISMATCH', diff, tol
    return 'AMBIGUOUS', diff, tol


def constant_provenance(text):
    """Named-constant (and mpmath.identify) matches for salient decimals —
    report-only leads: 'reported 4.6692 is Feigenbaum delta to 4 decimals'."""
    out = []
    for m in re.finditer(r'(?<![\d.])(-?\d+\.\d{3,})(?![\d])', text):
        ctx = text[max(0, m.start() - 12):m.start()]
        if _METRIC_CTX.search(ctx):
            continue                      # p=/r=/AUC= style measurement context
        x = float(m.group(1))
        d = _decimals(m.group(1))
        tol = 0.51 * 10 ** (-d)
        for name, kval in NAMED.items():
            if abs(abs(x) - kval) <= tol:
                out.append({'stated': x, 'matches': name, 'value': kval,
                            'decimals': d})
                break
        else:
            if HAVE_MPMATH and 1e-3 < abs(x) < 1e5 and d >= 4:
                try:
                    ident = mpmath.identify(
                        abs(x), ['pi', 'e', 'ln(2)', 'sqrt(2)', 'sqrt(3)',
                                 '2**(1/3)', '3**(1/3)', 'euler', 'catalan'],
                        tol=tol / max(abs(x), 1e-9))
                except Exception:
                    ident = None
                if ident and len(ident) <= 32 and not re.search(r'\d{3,}', ident):
                    out.append({'stated': x, 'matches': ident,
                                'via': 'mpmath.identify', 'decimals': d})
    return out


# ── claim iteration & evidence writing ───────────────────────────────────────

def claim_texts(conn, claim_id):
    """claim_summary + the top few support-side findings, deduped."""
    texts = []
    row = conn.execute("SELECT claim_summary, domain FROM knowledge_claims WHERE id=?",
                       (claim_id,)).fetchone()
    domain = row[1] if row else None
    if row and row[0]:
        texts.append(row[0])
    for (kf,) in conn.execute(
            "SELECT key_finding FROM claim_evidence WHERE claim_id=? AND key_finding "
            "IS NOT NULL AND evidence_type IN ('support','refute') "
            "ORDER BY id DESC LIMIT 4", (claim_id,)):
        if kf and kf not in texts:
            texts.append(kf)
    return texts, domain


def ensure_schema(conn):
    """Own ledger table, mirroring novelty_audits / adversarial_replications /
    dispute_arbitrations — every lane records to its own table, NOT to
    claim_evidence. This is not optional bookkeeping: the contradiction_detector
    cron sweeps `claim_evidence WHERE worker_result_id IS NULL` every 15 min as
    orphans, so a non-worker row jammed into claim_evidence is deleted within the
    quarter-hour. A dedicated table survives, stays queryable next to the other
    lanes, and keeps claim_evidence pure (worker-backed rows only)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analytic_verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL,
            verdict TEXT NOT NULL,           -- VERIFIED | MISMATCH
            stated REAL, expr TEXT, value REAL, diff REAL, tol REAL,
            domain TEXT,
            source_finding TEXT,             -- the finding text the identity came from
            enqueued_recompute INTEGER DEFAULT 0,
            model TEXT DEFAULT 'symbolic_verifier',
            created_at REAL NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_av_claim ON analytic_verifications(claim_id)")
    conn.commit()


def already_processed(conn, claim_id):
    return conn.execute(
        "SELECT 1 FROM analytic_verifications WHERE claim_id=? LIMIT 1",
        (claim_id,)).fetchone() is not None


def record_verification(conn, claim_id, verdict, domain, stated, expr, val,
                        diff, tol, source, enqueued=0):
    """Append a verdict row to the analytic ledger. POSTERIOR-NEUTRAL by design:
    a verified identity (e.g. "ln(64)=4.159") is usually an incidental constant,
    orthogonal to whether the claim's DIRECTIONAL hypothesis holds — the source
    finding may even have NARROWED the claim. So this NEVER touches the claim's
    posterior, tier, counts, or summary; it is an audit trail the novelty/dispute
    lanes can consult. Timestamp is epoch float (time.time()), never ISO."""
    conn.execute(
        "INSERT INTO analytic_verifications (claim_id, verdict, stated, expr, "
        "value, diff, tol, domain, source_finding, enqueued_recompute, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (claim_id, verdict, stated, expr, val, diff, tol, domain,
         (source or '')[:400], enqueued, time.time()))
    conn.commit()          # short txn; claim maturity intentionally untouched


def enqueue_recompute(claim_id, stated, expr, val):
    note = (f"ANALYTIC MISMATCH: finding states {stated} = {expr}, but {expr} "
            f"evaluates to {val:.10g}. Recompute the quantity from scratch "
            f"(exact arithmetic where possible) and report which figure is "
            f"correct; a prose typo and a wrong result need different fixes.")
    r = subprocess.run(['python3', ENQUEUER, '--claim', str(claim_id),
                        '--note', note], capture_output=True, text=True, timeout=180)
    return r.returncode == 0, (r.stdout + r.stderr).strip()[-400:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='write evidence and enqueue recomputes (default: dry-run)')
    ap.add_argument('--claim', type=int, help='verify a single claim id')
    ap.add_argument('--batch', type=int, default=25, help='max claims per run')
    ap.add_argument('--max-enqueue', type=int, default=3,
                    help='cap recompute enqueues per run (only with --enqueue-mismatch)')
    ap.add_argument('--enqueue-mismatch', action='store_true',
                    help='OPT-IN: enqueue a recompute for each MISMATCH. Off by '
                         'default — worker prose uses loose/inverse "≈" notation '
                         '(e.g. "4/7 ≈ 1.75x") that reads as a false mismatch, so '
                         'mismatches are report-only leads for review unless asked')
    ap.add_argument('--status', default='ESTABLISHED,REPLICATED',
                    help='claim_status tiers to scan (comma-separated)')
    ap.add_argument('--force', action='store_true',
                    help='re-scan claims already in the analytic_verifications ledger')
    args = ap.parse_args()

    mode = 'rw' if args.apply else 'ro'
    conn = sqlite3.connect(f'file:{DB}?mode={mode}', uri=True, timeout=30)
    conn.execute('PRAGMA busy_timeout=30000')
    if args.apply:
        ensure_schema(conn)

    if args.claim:
        ids = [args.claim]
    else:
        tiers = [s.strip() for s in args.status.split(',') if s.strip()]
        q = ("SELECT id FROM knowledge_claims WHERE claim_status IN (%s) AND "
             "COALESCE(is_meta,0)=0 ORDER BY id DESC" %
             ','.join('?' * len(tiers)))
        ids = [r[0] for r in conn.execute(q, tiers)]

    report = {'run_at': time.time(), 'apply': args.apply,
              'mpmath': HAVE_MPMATH, 'scanned': 0, 'skipped_already': 0,
              'verified': [], 'mismatches': [], 'ambiguous': [], 'provenance': []}
    enqueued = 0
    for cid in ids:
        if report['scanned'] >= args.batch:
            break
        if args.apply and not args.force and already_processed(conn, cid):
            report['skipped_already'] += 1
            continue
        texts, domain = claim_texts(conn, cid)
        if not texts:
            continue
        report['scanned'] += 1
        blob = '\n'.join(texts)
        verified_here = False
        for stated, dec, expr, val in extract_equalities(blob):
            verdict, diff, tol = classify(stated, dec, val)
            entry = {'claim': cid, 'stated': stated, 'expr': expr,
                     'value': round(val, 10), 'diff': diff, 'verdict': verdict}
            if verdict == 'VERIFIED':
                if args.apply and not verified_here:
                    record_verification(conn, cid, 'VERIFIED', domain, stated,
                                        expr, val, diff, tol, blob)
                report['verified'].append(entry)
                verified_here = True
                break        # one verified identity per claim is enough
            elif verdict == 'MISMATCH':
                ok = 0
                if args.apply and args.enqueue_mismatch and enqueued < args.max_enqueue:
                    done, out = enqueue_recompute(cid, stated, expr, val)
                    entry['enqueued'], entry['enqueuer_out'] = done, out
                    ok = 1 if done else 0
                    enqueued += ok
                if args.apply:
                    record_verification(conn, cid, 'MISMATCH', domain, stated,
                                        expr, val, diff, tol, blob, enqueued=ok)
                report['mismatches'].append(entry)
            else:
                report['ambiguous'].append(entry)
        for p in constant_provenance(blob):
            p['claim'] = cid
            report['provenance'].append(p)
    conn.close()

    # Persist a report ONLY when something actionable was found. At a 15-minute
    # cadence a file-per-run would be pure litter — steady state (everything
    # already in the ledger) finds nothing new, so it writes nothing and just
    # prints the one-line summary the cron delivers.
    path = '(nothing new — no report written)'
    if report['verified'] or report['mismatches']:
        os.makedirs(REPORT_DIR, exist_ok=True)
        # millisecond stamp: second-resolution names collide when the lane is
        # run several times inside one second (manual back-to-back invocations).
        path = os.path.join(REPORT_DIR, f"run_{int(time.time() * 1000)}.json")
        with open(path, 'w') as f:
            json.dump(report, f, indent=1, default=str)
    print(f"scanned {report['scanned']} claims "
          f"({report['skipped_already']} already in ledger) · "
          f"{len(report['verified'])} verified · {len(report['mismatches'])} "
          f"mismatches ({enqueued} recomputes enqueued) · "
          f"{len(report['ambiguous'])} ambiguous · "
          f"{len(report['provenance'])} constant-provenance leads · "
          f"mpmath={'yes' if HAVE_MPMATH else 'no'} · report {path}")


if __name__ == '__main__':
    main()
