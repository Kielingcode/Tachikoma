"""CanaryRunner + CanaryEvaluator(FR-27/28,P1 Stage 2)。

- Runner:同任务配对 with-E / without-E;arm 编码配对键(canary_with#p3);
  **学习隔离双层**:learn=False(runner 纪律)+ arm 前缀在 store.relearn 被排除(机制)。
- Evaluator:**纯函数**——对 arm LIKE 'canary%' 的 episodes 确定性重算 verdict 与
  `causal_verified`(P16:canary runs 即 raw_events,全量重放可重建 verified;
  status_history 是审计轨迹而非事件源)。
- 升级判据(G-1,bypassable 分流):paired median step_delta > 噪声阈值
  ∧ adoption rate ≥ θ_adopt ∧ near-miss 在容忍内;fail→pass = 强证据、非必要条件。
"""

from __future__ import annotations

import json
import re
import statistics
import uuid
from datetime import datetime, timezone
from pathlib import Path

from tachicoma.path_classifier import adoption_record
from tachicoma.runner import run_episode

_ARM = re.compile(r"^(canary(?:_nm)?)_(with|without)#p(\d+)$")


def run_canary_pairs(store, variant_ids: list[str], model: str, workspace_root: Path,
                     pair_offset: int = 0, nearmiss: bool = False) -> list[dict]:
    prefix = "canary_nm" if nearmiss else "canary"
    out = []
    for i, vid in enumerate(variant_ids):
        p = pair_offset + i
        w = run_episode(store, vid, arm=f"{prefix}_with#p{p}", model=model,
                        memory_on=True, workspace_root=workspace_root, learn=False)
        wo = run_episode(store, vid, arm=f"{prefix}_without#p{p}", model=model,
                         memory_on=False, workspace_root=workspace_root, learn=False)
        out.append({"pair": p, "variant": vid, "with": w, "without": wo})
    return out


def batch_episode_ids(pairs: list[dict]) -> set:
    """从 run_canary_pairs 结果取本批 episode-id 集——传给 evaluate(episode_ids=...)
    以隔离继承快照 store 上的旧批 canary(P2.1 命名空间隔离)。"""
    ids = set()
    for p in pairs:
        for side in ("with", "without"):
            r = p.get(side)
            if r:
                ids.add(r["episode_id"])
    return ids


def evaluate(store, memory_id: str, *, step_delta_gate: float, theta_adopt: float,
             nearmiss_max_neg_flips: int = 0, episode_ids: set | None = None) -> dict:
    """纯函数:store(canary episodes)+ 阈值 → verdict。可重复调用,结果确定。

    评估范围显式(P2.1 修正):`episode_ids` 给定时**只聚合该集合内**的 canary
    episodes——继承快照 store(如 P1→P2 wasteful 副本)携带旧批 canary,且
    pair-id 命名空间跨批复用,隐式全店扫描 `canary%` 会把旧批污染进聚合
    (P2 Stage D 实测 n_pairs=9 假判定)。省略时退回全店扫描(单批 store 安全)。
    """
    item = store.con.execute(
        "SELECT * FROM memory_items WHERE memory_id=?", (memory_id,)).fetchone()
    trigger = json.loads(item["trigger_json"]).get("after_edit", "")
    action = json.loads(item["action_json"]).get("must_run", "")

    pairs: dict[tuple, dict] = {}
    for row in store.con.execute(
            "SELECT * FROM episodes WHERE arm LIKE 'canary%' ORDER BY started_at"):
        if episode_ids is not None and row["episode_id"] not in episode_ids:
            continue
        m = _ARM.match(row["arm"])
        if not m:
            continue
        kind_prefix, side, pid = m.group(1), m.group(2), int(m.group(3))
        pairs.setdefault((kind_prefix, pid), {})[side] = row

    deltas, pos_flips, neg_flips = [], 0, 0
    adopt_hits, adopt_total = 0, 0
    nm_neg_flips, nm_inflation = 0, []

    for (kind, _pid), pr in sorted(pairs.items()):
        if "with" not in pr or "without" not in pr:
            continue
        w, wo = pr["with"], pr["without"]
        if kind == "canary":
            deltas.append((wo["cost_steps"] or 0) - (w["cost_steps"] or 0))
            pos_flips += int(not wo["eventual_success"] and bool(w["eventual_success"]))
            neg_flips += int(bool(wo["eventual_success"]) and not w["eventual_success"])
            ep, _, _ = store.episode_view(w["episode_id"])
            rec = adoption_record(ep, trigger, action)
            adopt_total += 1
            adopt_hits += int(rec.adopted)
        else:  # near-miss
            nm_neg_flips += int(bool(wo["eventual_success"]) and not w["eventual_success"])
            nm_inflation.append((w["cost_steps"] or 0) - (wo["cost_steps"] or 0))

    median_delta = statistics.median(deltas) if deltas else None
    adoption_rate = (adopt_hits / adopt_total) if adopt_total else None
    accept = (
        median_delta is not None and median_delta > step_delta_gate
        and adoption_rate is not None and adoption_rate >= theta_adopt
        and nm_neg_flips <= nearmiss_max_neg_flips
    )
    return {
        "memory_id": memory_id, "accept": accept,
        "n_pairs": len(deltas), "median_step_delta": median_delta,
        "step_delta_gate": step_delta_gate,
        "pos_flips": pos_flips, "neg_flips": neg_flips,   # 强证据,记录;非必要条件(G-1)
        "adoption_rate": adoption_rate, "theta_adopt": theta_adopt,
        "nearmiss_neg_flips": nm_neg_flips,
        "nearmiss_step_inflation": nm_inflation,
    }


def apply_verdict(store, verdict: dict) -> bool:
    """确定性落账(幂等:状态已一致则 no-op)。仅 accept → verified;
    reject 不降级——降级权属于 demotion 机制(负向证据),canary 拒绝只是不升。"""
    mid = verdict["memory_id"]
    row = store.con.execute(
        "SELECT status, causal_verified FROM memory_items WHERE memory_id=?", (mid,)).fetchone()
    if not verdict["accept"]:
        return False
    if row["causal_verified"] and row["status"] == "active_verified":
        return False  # 已一致,幂等 no-op
    now = datetime.now(timezone.utc).isoformat()
    job = f"canary_{uuid.uuid4().hex[:8]}"
    store.con.execute(
        "UPDATE memory_items SET causal_verified=1, status='active_verified', updated_at=?"
        " WHERE memory_id=? AND status LIKE 'active%'", (now, mid))
    store.con.execute(
        "INSERT INTO status_history (memory_id, old_status, new_status, reason,"
        " evidence_snapshot_json, job_id, created_at) VALUES (?,?,?,?,?,?,?)",
        (mid, row["status"], "active_verified",
         f"canary: median_delta={verdict['median_step_delta']} "
         f"(gate {verdict['step_delta_gate']}), adoption={verdict['adoption_rate']}, "
         f"nm_neg={verdict['nearmiss_neg_flips']}",
         json.dumps(verdict), job, now))
    store.con.commit()
    return True


def rebuild_verified(store, memory_id: str, *, step_delta_gate: float,
                     theta_adopt: float) -> dict:
    """P16 重放入口:从 raw_events(canary episodes)确定性重建 verified。"""
    v = evaluate(store, memory_id, step_delta_gate=step_delta_gate, theta_adopt=theta_adopt)
    apply_verdict(store, v)
    return v
