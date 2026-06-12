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
INERT_INJECTIONS_K = 3         # 惰性剪枝:最近 K 次注入连续未被采纳 → deprecated(FR-25/S13)


def evaluate_rebirth(current_status: str, evidence_chrono: list[dict]) -> str:
    """FR-22b 重生车道(P2)——**只看死亡点之后证据**的纯函数。

    死亡点锚 = 最后一条负向证据(负向驱动的死亡;evaluate_demotion 的触发
    必然以负向为前提,故"最后负向之后无负向且达出生门"等价于
    "post-death 窗口干净")。inert 死亡(零负向)的精确锚点随 S14 live 定,
    P2-core 不依赖。门槛与出生门对称:post-death organic fam≥2 ∧ s≥2
    (隐含无负向——有新负向则它成为新锚)。
    deprecated → candidate;disputed → active_correlational;
    **绝不直回 verified**(causal_verified 由调用方清零,必须重过 canary)。
    """
    if current_status not in ("deprecated", "disputed"):
        return current_status
    last_neg = max((i for i, e in enumerate(evidence_chrono) if e["polarity"] < 0),
                   default=None)
    if last_neg is None:
        return current_status
    post = evidence_chrono[last_neg + 1:]
    organic = [e for e in post
               if e["polarity"] > 0 and e.get("evidence_source") == "organic_task"]
    s = len(organic)
    fams = {e.get("family_id") for e in organic if e.get("family_id")}
    if s >= 2 and len(fams) >= 2:
        return "candidate" if current_status == "deprecated" else "active_correlational"
    return current_status


def evaluate_inert(current_status: str, injection_adopted_seq: list[bool]) -> str:
    """FR-25 惰性剪枝(纯函数,不依赖 canary):active memory 最近 K 次注入
    **连续未被采纳** → deprecated(inert,直接弧 active → deprecated)。

    `injection_adopted_seq` = 该 memory 全部注入事件的采纳布尔序列
    (按 episodes.started_at 升序;learning-excluded arms 不计)。
    adoption_support 是抵抗信号:被采纳即重置连续计数——本规则只剪
    "反复递到 agent 面前都不被用"的死重,不剪正在被使用的 memory。
    "采纳但无 outcome 差异"分支依赖配对测量,P1 不实现(P2 决定)。
    """
    if not current_status.startswith("active"):
        return current_status
    recent = injection_adopted_seq[-INERT_INJECTIONS_K:]
    if len(recent) >= INERT_INJECTIONS_K and not any(recent):
        return "deprecated"
    return current_status


def evaluate_demotion(current_status: str, evidence_chrono: list[dict],
                      episodes_since_last_evidence: int = 0) -> str:
    """Dispute / deprecate —— **证据集 + 观察流的纯函数**(S4 幂等保命约束)。

    输入为该 memory 全部 evidence(dict 含 polarity, started_at),
    **已按 (episodes.started_at, polarity) 升序**(不可变锚 + 确定性 tie-break:
    同 episode 内负向在前、正向在后——保守,正向在窗口里存活最久;
    禁用 claims.created_at——重放会换)。

    观察期推进(P1 Stage 3b 实测修正):disputed memory 被检索压制后不再获得
    新证据,纯证据窗口会**冻结**,deprecate 永不可达。修正 = 同 repo 后续
    learning-eligible episodes 的流逝本身就是"无回升"观察,以空槽(polarity=0)
    并入观察流。仍是 raw_events 的确定性函数(episodes 不可变),重放幂等。
    """
    negatives = sum(1 for e in evidence_chrono if e["polarity"] < 0)

    if current_status.startswith("active") and negatives >= DISPUTE_NEGATIVES:
        current_status = "disputed"

    if current_status == "disputed":
        stream = list(evidence_chrono) + [{"polarity": 0}] * episodes_since_last_evidence
        recent = stream[-DEPRECATE_WINDOW_M:]
        # 观察期判定:dispute 成立后,最近 M 个观察(证据或流逝 episode)
        # 无任何正向 → 无回升 → deprecated
        if len(recent) >= DEPRECATE_WINDOW_M and not any(e["polarity"] > 0 for e in recent):
            return "deprecated"

    return current_status
