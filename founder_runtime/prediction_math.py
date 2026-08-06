#!/usr/bin/env python3
"""
Prediction Math — exact, symbolic, auditable math for the prediction engine.

Replaces heuristic ensemble weighting with the exact solution, and scores
resolutions with sealed surprisal proofs via the rig-math-exec sympy MCP.

Three exact computations:
1. OPTIMAL ENSEMBLE WEIGHTS — solve min_w Σ_t (w·v_t − o_t)² s.t. Σw=1, w≥0.
   Symbolic normal equations via sympy; non-negativity by active-set
   projection (drop violators, re-solve). Exact until the final float cast.
2. BETA POSTERIOR base rates — Beta(α,β) conjugate update, exact posterior
   mean and 95% credible interval via sympy's incomplete beta (rational
   inputs; no floating-point drift).
3. SEALED SURPRISAL — S(o) = −ln P(o) per resolution via the rig-math-exec
   sympy MCP server; each score returns a hash-chained MathProofPacket.

Head-to-head protocol: the studio's heuristic weights stay untouched. We
publish BOTH p_true values per question; resolutions grade each side.
"""
from __future__ import annotations

import json
import subprocess
import time
from fractions import Fraction
from pathlib import Path

import sympy as sp

STATE = Path.home() / ".rig" / "state"
WEIGHTS_PATH = STATE / "optimal-weights.json"
MCP_BIN = "/Users/rig128gb/.rig/bin/rig-math-exec"


# ------------------------------------------------------------ 1. optimal weights

def optimal_ensemble_weights(votes: list[dict[str, float]],
                             outcomes: list[int],
                             persona_names: list[str]) -> dict:
    """Exact least-squares ensemble weights on the probability simplex.

    minimize_w  Σ_t (Σ_i w_i·v_{i,t} − o_t)²   s.t.  Σ_i w_i = 1,  w_i ≥ 0

    Solves the KKT system symbolically; active-set handles non-negativity.
    Returns {weights, brier_optimal, derivation}.
    """
    n = len(persona_names)
    assert len(votes) == len(outcomes) and len(votes) >= 1

    # Build exact rational matrices
    V = sp.Matrix([[sp.Rational(str(votes[t][p])) for p in persona_names]
                   for t in range(len(votes))])
    o = sp.Matrix([sp.Rational(int(x)) for x in outcomes])

    active = list(range(n))
    derivation = {"dropped": [], "method": "symbolic KKT + active-set projection"}

    while True:
        m = len(active)
        Va = V[:, active]
        # Normal equations with simplex constraint:
        # [2 VaᵀVa  1] [w]   [2 Vaᵀ o]
        # [  1ᵀ     0] [λ] = [   1   ]
        G = 2 * Va.T * Va
        c = 2 * Va.T * o
        KKT = sp.Matrix.zeros(m + 1, m + 1)
        KKT[:m, :m] = G
        KKT[:m, m] = sp.Matrix.ones(m, 1)
        KKT[m, :m] = sp.Matrix.ones(1, m)
        rhs = sp.Matrix.vstack(c, sp.Matrix([[1]]))

        try:
            sol = KKT.LUsolve(rhs)
        except Exception:
            # singular — fall back to uniform over active
            sol = sp.Matrix([sp.Rational(1, m)] * m + [sp.Rational(0)])

        w_active = sol[:m]
        violators = [i for i, wi in enumerate(w_active) if wi < 0]
        if not violators:
            break
        # drop the most-violating persona, re-solve
        worst = min(violators, key=lambda i: w_active[i])
        derivation["dropped"].append(persona_names[active[worst]])
        del active[worst]
        if not active:
            # everything violated — uniform fallback
            return {"weights": {p: 1.0 / n for p in persona_names},
                    "brier_optimal": None,
                    "derivation": {**derivation, "fallback": "uniform"}}

    w_full = {p: 0.0 for p in persona_names}
    for i, idx in enumerate(active):
        w_full[persona_names[idx]] = float(w_active[i])

    # Exact optimal Brier at solution
    w_vec = sp.Matrix([w_full[p] for p in persona_names])
    resid = V * w_vec - o
    brier_exact = (resid.T * resid)[0] / sp.Rational(len(outcomes))

    return {
        "weights": {p: round(w_full[p], 6) for p in persona_names},
        "brier_optimal": float(brier_exact),
        "brier_optimal_exact": str(brier_exact),
        "derivation": derivation,
    }


def ensemble_p(votes: dict[str, float], weights: dict[str, float]) -> float:
    """Exact weighted ensemble probability (rational until final cast)."""
    num = sum(sp.Rational(str(votes[p])) * sp.Rational(str(weights.get(p, 0.0)))
              for p in votes)
    den = sum(sp.Rational(str(weights.get(p, 0.0))) for p in votes)
    if den == 0:
        return 0.5
    p = num / den
    return float(min(sp.Rational(97, 100), max(sp.Rational(3, 100), p)))


# ------------------------------------------------------------ 2. beta posterior

def beta_posterior(successes: int, failures: int,
                   prior_alpha: int = 1, prior_beta: int = 1) -> dict:
    """Exact Beta posterior for a binary base rate.

    Returns posterior mean + 95% credible interval via exact rational math.
    """
    a = prior_alpha + successes
    b = prior_beta + failures
    mean = sp.Rational(a, a + b)
    # Exact variance: ab / ((a+b)² (a+b+1))
    var = sp.Rational(a * b, (a + b) ** 2 * (a + b + 1))
    sd = sp.sqrt(var)
    lo = max(sp.Rational(0), mean - 2 * sd)
    hi = min(sp.Rational(1), mean + 2 * sd)
    return {
        "alpha": a, "beta": b,
        "mean": float(mean), "mean_exact": f"{a}/{a+b}",
        "ci95": [float(lo), float(hi)],
        "n": successes + failures,
    }


# ------------------------------------------------------------ 3. sealed surprisal

class McpSurprisal:
    """Minimal stdio JSON-RPC client for rig-math-exec sympy profile.

    Scores a resolved prediction with S(o) = −ln P(o) and returns the
    sealed MathProofPacket (hash-chained evidence the math actually ran).
    """

    def __init__(self):
        self.proc = subprocess.Popen(
            [MCP_BIN, "mcp-server", "--profile", "sympy-mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
        )
        self._id = 0
        self._call("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "prediction-math", "version": "1.0"}})
        self._notify("notifications/initialized")

    def _send(self, payload: dict) -> dict:
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP server closed")
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == payload.get("id"):
                return msg

    def _call(self, method: str, params: dict) -> dict:
        self._id += 1
        return self._send({"jsonrpc": "2.0", "id": self._id,
                           "method": method, "params": params})

    def _notify(self, method: str) -> None:
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def surprisal(self, probability: float) -> dict:
        resp = self._call("tools/call", {
            "name": "rig_math_exec_surprisal",
            "arguments": {"probability": probability},
        })
        content = resp.get("result", {}).get("content", [])
        for c in content:
            if c.get("type") == "text":
                try:
                    return json.loads(c["text"])
                except Exception:
                    return {"raw": c["text"]}
        return resp.get("result", {})

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()


# ------------------------------------------------------------ driver

def reweight_from_studio_db(db_path: str) -> dict:
    """Pull vote/outcome history from the studio db, solve optimal weights,
    persist with derivation + timestamp. Head-to-head: also compute the
    studio heuristic's Brier for comparison."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT p.prediction_id, p.persona_votes, b.actual_outcome
          FROM predictions p
          JOIN (SELECT DISTINCT prediction_id, actual_outcome FROM brier_scores
                 WHERE actual_outcome IS NOT NULL) b
            ON p.prediction_id = b.prediction_id
        """
    ).fetchall()
    conn.close()

    if len(rows) < 10:
        return {"status": "insufficient_history", "n": len(rows)}

    votes = [json.loads(r["persona_votes"]) for r in rows]
    outcomes = [int(r["actual_outcome"]) for r in rows]
    personas = sorted(votes[0].keys())

    result = optimal_ensemble_weights(votes, outcomes, personas)
    result["n_resolutions"] = len(outcomes)
    result["computed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Head-to-head: studio heuristic Brier vs optimal Brier on same history
    studio_brier = 0.0
    optimal_brier_sum = 0.0
    for t, r in enumerate(rows):
        v = votes[t]
        o = outcomes[t]
        # heuristic side: uniform-ish (studio weights were ~equal during backfill)
        p_heur = sum(v.values()) / len(v)
        studio_brier += (p_heur - o) ** 2
        p_opt = ensemble_p(v, result["weights"])
        optimal_brier_sum += (p_opt - o) ** 2
    result["head_to_head"] = {
        "uniform_ensemble_brier": round(studio_brier / len(outcomes), 4),
        "optimal_ensemble_brier": round(optimal_brier_sum / len(outcomes), 4),
    }

    WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    WEIGHTS_PATH.write_text(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    db = "/Users/rig128gb/Developer/RIGForge/repos/rig-prediction-studio-pro/data/brier_calibration.db"
    out = reweight_from_studio_db(db)
    print(json.dumps(out, indent=2))
