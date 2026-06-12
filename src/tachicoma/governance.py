"""Governance — derived belief + the P0 counting promotion gate (FR-19/FR-23).

P0 gate (satisfiability-checked):  families >= 2  AND  S >= 2  AND  S >= 3F  AND actionable.
Preponderance instead of zero-veto: one flaky contradiction does not block promotion;
sustained contradiction does. Beta posterior machinery activates at P1 with calibrated θ.
The generator's fact_oracle must never appear here (FR-43 firewall — this module
must not import tachicoma.oracle_eval).
"""

from __future__ import annotations


def recompute_belief(cur, memory_id: str) -> dict:
    """Full recompute from evidence_links + episodes (never incremental — P4/P16)."""
    rows = cur.execute(
        "SELECT e.polarity, ep.family_id FROM evidence_links e"
        " JOIN claims c ON e.claim_id=c.claim_id"
        " JOIN episodes ep ON c.episode_id=ep.episode_id"
        " WHERE e.memory_id=?", (memory_id,)).fetchall()
    support = sum(1 for r in rows if r["polarity"] > 0)
    contra = sum(1 for r in rows if r["polarity"] < 0)
    families = len({r["family_id"] for r in rows if r["polarity"] > 0 and r["family_id"]})
    belief = {"support": support, "contra": contra, "families": families}
    cur.execute(
        "INSERT INTO belief_states (memory_id, support_count, contradiction_count,"
        " distinct_task_family, computed_from_version, first_seen, last_seen)"
        " VALUES (?,?,?,?,?,datetime('now'),datetime('now'))"
        " ON CONFLICT(memory_id) DO UPDATE SET support_count=excluded.support_count,"
        " contradiction_count=excluded.contradiction_count,"
        " distinct_task_family=excluded.distinct_task_family,"
        " computed_from_version=excluded.computed_from_version,"
        " last_seen=datetime('now')",
        (memory_id, support, contra, families, "gate-v1"))
    return belief


def evaluate_gate(current_status: str, belief: dict) -> str:
    """Counting rule. Cascade-aware: items that no longer satisfy the gate demote."""
    s, f, fam = belief["support"], belief["contra"], belief["families"]
    qualifies = fam >= 2 and s >= 2 and s >= 3 * f
    if current_status == "candidate" and qualifies:
        return "active_correlational"
    if current_status == "active_correlational" and not qualifies:
        return "candidate"   # demote on evidence loss (relearn cascade, FR-18)
    return current_status
