"""Governance — derived belief + the P0 counting promotion gate (FR-19/FR-23).

P0 gate (satisfiability-checked):  families >= 2  AND  S >= 2  AND  S >= 3F  AND actionable.
Preponderance instead of zero-veto: one flaky contradiction does not block promotion;
sustained contradiction does. Beta posterior machinery activates at P1 with calibrated θ.
The generator's fact_oracle must never appear here (FR-43 firewall — this module
must not import tachicoma.oracle_eval).
"""

from __future__ import annotations


def recompute_belief(cur, memory_id: str) -> dict:
    """Full recompute from evidence_links + episodes (never incremental — P4/P16).

    Evidence-class boundary(P0b 裁决):
    - independent_support / families:只数 evidence_source='organic_task' 的正向
      (memory-off 独立发现)——这是 birth/promotion 的唯一燃料;
    - adoption_support:memory-on adopted+success 的正向,单独累计
      (utility / demotion 抵抗用,P1 裁决权重);
    - contradiction:负向无论来源全计(P9 不对称性:注入后仍失败是可信负信号)。
    """
    import json as _json

    rows = cur.execute(
        "SELECT e.polarity, e.evidence_source, ep.family_id, ep.model_version"
        " FROM evidence_links e"
        " JOIN claims c ON e.claim_id=c.claim_id"
        " JOIN episodes ep ON c.episode_id=ep.episode_id"
        " WHERE e.memory_id=?", (memory_id,)).fetchall()
    support = sum(1 for r in rows
                  if r["polarity"] > 0 and r["evidence_source"] == "organic_task")
    adoption = sum(1 for r in rows
                   if r["polarity"] > 0 and r["evidence_source"] == "adoption_outcome")
    contra = sum(1 for r in rows if r["polarity"] < 0)
    families = len({r["family_id"] for r in rows
                    if r["polarity"] > 0 and r["evidence_source"] == "organic_task"
                    and r["family_id"]})
    # G-5(P1):model_version 只进 per_context 元数据(per-model diagnostic view 数据源)。
    # belief 仍按 repo/fact 聚合;retrieval 不读 per_context_json——跨模型证据共享
    # (P0b 实证)不被分桶切碎;分桶/加权/routing 是 P2 决定。
    by_model: dict[str, dict] = {}
    for r in rows:
        m = by_model.setdefault(r["model_version"] or "unknown", {"s": 0, "f": 0})
        if r["polarity"] > 0:
            m["s"] += 1
        else:
            m["f"] += 1
    belief = {"support": support, "contra": contra, "families": families,
              "adoption_support": adoption}
    cur.execute(
        "INSERT INTO belief_states (memory_id, support_count, contradiction_count,"
        " distinct_task_family, per_context_json, computed_from_version,"
        " first_seen, last_seen)"
        " VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))"
        " ON CONFLICT(memory_id) DO UPDATE SET support_count=excluded.support_count,"
        " contradiction_count=excluded.contradiction_count,"
        " distinct_task_family=excluded.distinct_task_family,"
        " per_context_json=excluded.per_context_json,"
        " computed_from_version=excluded.computed_from_version,"
        " last_seen=datetime('now')",
        (memory_id, support, contra, families,
         _json.dumps({"adoption_support": adoption, "by_model": by_model}), "gate-v2"))
    return belief


def per_model_view(con) -> list[dict]:
    """Per-model diagnostic view(G-5,只读诊断;不参与 gate / retrieval)。"""
    import json as _json

    out = []
    for r in con.execute(
            "SELECT m.memory_id, m.canonical_key, m.status, b.per_context_json"
            " FROM memory_items m JOIN belief_states b ON m.memory_id=b.memory_id"):
        ctx = _json.loads(r["per_context_json"] or "{}")
        for model, sf in ctx.get("by_model", {}).items():
            out.append({"memory_id": r["memory_id"], "canonical_key": r["canonical_key"],
                        "status": r["status"], "model_version": model, **sf})
    return out


def evaluate_gate(current_status: str, belief: dict) -> str:
    """Counting rule(晋升)+ 证据丢失级联(降回 candidate)。

    职责边界(P1 修正):矛盾增长(F 上升)的处理权属于 evaluate_demotion
    (active → disputed,FR-22/S7);gate 的级联降级只管【证据丢失】
    (relearn 替换后 S/families 跌破)——否则负向证据会把 active 降回 candidate,
    dispute 机制永远轮不到。
    """
    s, f, fam = belief["support"], belief["contra"], belief["families"]
    if current_status == "candidate" and fam >= 2 and s >= 2 and s >= 3 * f:
        return "active_correlational"
    if current_status == "active_correlational" and (s < 2 or fam < 2):
        return "candidate"   # 证据丢失(FR-18 级联);矛盾走 demotion
    return current_status


# 降级参数(P1;数值闸门可满足性:dispute 门与 plan 3a 通过线一致)
DISPUTE_NEGATIVES = 2          # 程序级负向 ≥2 → disputed(active_* 均适用,verified 非免死金牌)
DEPRECATE_WINDOW_M = 3         # 观察期:最近 M 条证据(按 episodes.started_at)无正向 → deprecated


def evaluate_demotion(current_status: str, evidence_chrono: list[dict]) -> str:
    """Dispute / deprecate —— **证据集的纯函数**(S4 幂等保命约束)。

    输入为该 memory 全部 evidence(dict 含 polarity, started_at),
    **已按 episodes.started_at 升序**(不可变锚;禁用 claims.created_at——重放会换)。
    不依赖墙钟、不依赖 relearn 执行顺序;同一证据集 → 同一状态,重放幂等。
    """
    negatives = sum(1 for e in evidence_chrono if e["polarity"] < 0)

    if current_status.startswith("active") and negatives >= DISPUTE_NEGATIVES:
        current_status = "disputed"

    if current_status == "disputed":
        recent = evidence_chrono[-DEPRECATE_WINDOW_M:]
        # 观察期判定:dispute 成立后,最近 M 条证据无任何正向 → 无回升 → deprecated
        if len(recent) >= DEPRECATE_WINDOW_M and not any(e["polarity"] > 0 for e in recent):
            return "deprecated"

    return current_status
