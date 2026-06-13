"""P2.1 Stage C — S10 + S15 主线(FR-8b 反馈 / VP organic 出生 / 数字门)。

前置:B1 rename 武装(driver 自检 b1_calibration.json gate.verdict)。
C1 VP 学习批(带 FR-8b Level 2 反馈):oracle fail → 后续注入密封反馈 → 探索验证工具
    → VP organic 出生晋升。前 2 run 兼 smoke:反馈不诱发探索(零 check 触碰且零 tools/
    list/read)→ kill-line 分支 b。
硬停止 + seed-fallback(默认开):无 organic VP → organic-S10 不执行、S15 NOT MEASURABLE;
    seeded-S10 继续(seeded VP 用 FR-9b 提取器在合成理想轨迹生成,与 organic 同构,
    标注非 governed)。
C2 held-out S10:memory-on VP-only 注入,数字门(baseline≥3/6 ∧ ≤1/6 ∧ 降≥2 ∧ 采纳≥0.8)。
C3 S15 组合 smoke:PD+VP co-注入,双轨采纳 / top-1=migrate / deprecated 不出场 / 诊断三字段。
"""

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tachicoma.extractor import extract
from tachicoma.path_classifier import Episode, adoption_record
from tachicoma.resolver import check_segments
from tachicoma.runner import events_to_actions, run_episode
from tachicoma.store import MemoryStore

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "spikes" / "p2_1"
STORE = OUT / "stage_c.sqlite"           # 自包含:从 verified.sqlite 起(S15 的 PD 半边)
SRC_STORE = ROOT / "spikes" / "p1" / "verified.sqlite"
RESULTS = OUT / "stage_c.json"
WS = Path("/tmp/p2_1_runs/c")
MODEL = "claude-sonnet-4-6"
CHECK_CMD = "python3 tools/check_contract.py"
MIGRATE_CMD = "python3 tools/migrate.py"

LEARN = [f"GR{i}@genb_v1" for i in (1, 3, 5, 2, 4, 6)]     # 跨字段交错
HELDOUT = [f"GR{i}@genb_v1" for i in (7, 8, 9, 10, 11, 12)]


def _oracle(store, eid):
    r = store.con.execute("SELECT payload_json FROM raw_events WHERE episode_id=?"
                          " AND event_type='DELAYED_CHECK_RESULT'", (eid,)).fetchone()
    return json.loads(r["payload_json"]).get("passed") if r else None


def _vp_row(store):
    return store.con.execute("SELECT memory_id, status FROM memory_items"
                             " WHERE memory_type='ValidationParity'").fetchone()


def _explored_validation(store, eid):
    """可观测判据:本 episode 是否运行 check_*.py 或 list/read tools/。"""
    for r in store.con.execute("SELECT event_type, payload_json FROM raw_events"
                               " WHERE episode_id=?", (eid,)):
        p = json.loads(r["payload_json"])
        cmd = p.get("command", "") or ""
        path = p.get("path", "") or ""
        if "check_" in cmd or "tools/" in cmd or path.startswith("tools/"):
            return True
    return False


def _seed_vp_memory(store):
    """seed-fallback:用 FR-9b 提取器在合成理想轨迹上生成 VP(与 organic 同构),
    手动落账 + 晋升为 active,标注 seeded-for-S10-isolation(非 governed 出生)。"""
    ideal = [
        {"step_idx": 1, "event_type": "TEST_RUN",
         "payload": {"command": "python3 -m pytest tests/ -q", "passed": True}},
        {"step_idx": 2, "event_type": "TEST_RUN",
         "payload": {"command": CHECK_CMD, "passed": False, "source": "check_tool"}},
        {"step_idx": 3, "event_type": "FILE_EDIT", "payload": {"path": "src/models.py"}},
        {"step_idx": 4, "event_type": "TEST_RUN",
         "payload": {"command": CHECK_CMD, "passed": True, "source": "check_tool"}},
        {"step_idx": 5, "event_type": "DELAYED_CHECK_RESULT", "payload": {"passed": True}},
    ]
    ep = Episode(actions=events_to_actions(ideal), eventual_success=True,
                 cost_steps=5, cost_tokens=10, memory_injected=False)
    vp = [c for c in extract(ep) if c.claim_type == "ValidationParity"]
    assert vp, "seeded ideal trajectory must yield a VP claim via FR-9b extractor"
    c = vp[0]
    from tachicoma.resolver import canonical_key, rival_key
    ck = canonical_key("ValidationParity", c.trigger, c.action)
    mid = f"seed_{uuid.uuid4().hex[:8]}"
    store.con.execute(
        "INSERT INTO memory_items (memory_id, memory_type, canonical_key, trigger_json,"
        " action_json, rival_key, scope_json, status, causal_verified) VALUES"
        " (?,?,?,?,?,?,?,?,0)",
        (mid, "ValidationParity", ck, json.dumps(c.trigger), json.dumps(c.action),
         rival_key("ValidationParity", "orderkit", c.trigger),
         json.dumps({"repo": "orderkit", "seeded_for": "S10-isolation"}),
         "active_correlational"))
    store.con.execute(
        "INSERT INTO belief_states (memory_id, support_count, contradiction_count,"
        " distinct_task_family, per_context_json, computed_from_version) VALUES"
        " (?,2,0,2,'{}','seed')", (mid,))
    store.con.commit()
    return mid


def main() -> None:
    gate = json.loads((OUT / "b1_calibration.json").read_text())["gate"]
    if not gate["verdict"].startswith("ARMED"):
        print(f"B1 未武装({gate['verdict']})——按 kill-line 拒跑 Stage C")
        return
    baseline_fs = int(gate["rename_false_success"].split("/")[0])
    baseline_n = int(gate["rename_false_success"].split("/")[1])

    import shutil
    if not STORE.exists():
        shutil.copy(SRC_STORE, STORE)
    store = MemoryStore(STORE)
    payload = {"learn": [], "heldout": [], "s15": [], "asserts": {}, "seed_fallback": None}

    # ---- C1 VP 学习批(FR-8b Level 2 反馈)----
    explored_any = False
    for i, ref in enumerate(LEARN, 1):
        print(f"[learn {i}/{len(LEARN)}] {ref}", flush=True)
        r = run_episode(store, ref, arm="memory_off", model=MODEL, memory_on=False,
                        workspace_root=WS / "learn", learn=True, feedback_level=2)
        explored = _explored_validation(store, r["episode_id"])
        explored_any = explored_any or explored
        vp = _vp_row(store)
        payload["learn"].append({"variant_id": r["variant_id"], "eventual": r["eventual"],
                                 "oracle": _oracle(store, r["episode_id"]),
                                 "explored_validation": explored,
                                 "vp_after": dict(vp) if vp else None})
        print(f"    local={r['eventual']} oracle={_oracle(store, r['episode_id'])}"
              f" explored={explored} vp={dict(vp) if vp else None}", flush=True)
        RESULTS.write_text(json.dumps(payload, indent=2))
        if i == 2 and not explored_any:
            print("前 2 run 零探索 → kill-line 分支 b(反馈未诱发探索)", flush=True)
        if vp and vp["status"].startswith("active"):
            print(f"VP organic 出生晋升于第 {i} run", flush=True)

    vp = _vp_row(store)
    organic_vp = bool(vp and vp["status"].startswith("active"))
    payload["asserts"]["vp_organic_promoted"] = organic_vp
    payload["asserts"]["feedback_induced_exploration"] = explored_any

    # ---- 硬停止 + seed-fallback ----
    vp_mid = None
    if organic_vp:
        vp_mid = vp["memory_id"]
    else:
        print("无 organic VP → S15 NOT MEASURABLE;启用 seeded-S10(默认)", flush=True)
        vp_mid = _seed_vp_memory(store)
        payload["seed_fallback"] = {"seeded_vp": vp_mid, "note":
                                    "FR-9b 提取器在合成理想轨迹生成,非 governed 出生"}
    RESULTS.write_text(json.dumps(payload, indent=2))

    # ---- C2 held-out S10(VP-only 注入)----
    # 受控:临时把非 VP 的 active memory 移出检索(VP-only 隔离)——
    # 用 scope 过滤:S10 批只注入 VP（k=1，且 retrieval 只有 VP always-on）
    fs_count, adopt_count = 0, 0
    for i, ref in enumerate(HELDOUT, 1):
        print(f"[s10 {i}/{len(HELDOUT)}] {ref}", flush=True)
        r = run_episode(store, ref, arm="s10_heldout", model=MODEL, memory_on=True,
                        workspace_root=WS / "s10", learn=False, k=1, feedback_level=2)
        eid = r["episode_id"]
        oracle = _oracle(store, eid)
        fs = bool(r["eventual"]) and oracle is False
        fs_count += int(fs)
        ep, _, _ = store.episode_view(eid)
        vp_adopted = any(a.kind in ("run", "test_run") and a.command
                         and CHECK_CMD in check_segments(a.command) for a in ep.actions)
        adopt_count += int(vp_adopted)
        payload["heldout"].append({"variant_id": r["variant_id"], "injected": r["injected"],
                                   "eventual_local": r["eventual"], "oracle": oracle,
                                   "false_success": fs, "vp_adopted": vp_adopted})
        print(f"    inj={r['injected']} local={r['eventual']} oracle={oracle}"
              f" fs={fs} vp_adopted={vp_adopted}", flush=True)
        RESULTS.write_text(json.dumps(payload, indent=2))

    n = len(payload["heldout"])
    line = "S10-organic" if organic_vp else "S10-seeded"
    payload["asserts"]["s10"] = {
        "report_line": line,
        "heldout_false_success": f"{fs_count}/{n}", "baseline": f"{baseline_fs}/{baseline_n}",
        "vp_adoption_rate": adopt_count / n if n else None,
        "gate_baseline_armed": baseline_fs >= 3,
        "gate_fs_le_1": fs_count <= 1,
        "gate_drop_ge_2": (baseline_fs - fs_count) >= 2,
        "gate_adoption_ge_0.8": (adopt_count / n if n else 0) >= 0.8,
        "PASS": (baseline_fs >= 3 and fs_count <= 1 and (baseline_fs - fs_count) >= 2
                 and (adopt_count / n if n else 0) >= 0.8),
        "caveat": None if organic_vp else "seeded VP,非 governed 出生;限定检索/采纳管线可工作,不外推 organic 同效",
    }
    RESULTS.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload["asserts"], indent=1, ensure_ascii=False))
    print(f"S15: {'可测(organic VP 存在)' if organic_vp else 'NOT MEASURABLE(无 organic VP)'}")


if __name__ == "__main__":
    main()
