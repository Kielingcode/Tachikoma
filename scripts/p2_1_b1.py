"""P2.1 Stage B1 — rename 主导校准批(FR-5b 家族分层 gate)。

只跑 primary=rename(GR 系),memory-off,统一 50% 门:
  base 6 → ≥3/6 武装进 Stage C;=2/6 补 6 至 12,≥6/12 武装;≤1/6 回炉(kill-line)。
v4 对照重测漂移 4 run;add-field 对照批 held-out 4 run(VP 注入 bounded-cost step≤6)。
store 血统:verified.sqlite 副本(migrate active = S15 的 PD 半边)。
arm 前缀 diagnostic_*(学习排除)。
"""

import json
import shutil
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tachicoma.runner import run_episode
from tachicoma.store import MemoryStore

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "spikes" / "p2_1"
OUT.mkdir(parents=True, exist_ok=True)
STORE = OUT / "genb.sqlite"
SRC_STORE = ROOT / "spikes" / "p1" / "verified.sqlite"
RESULTS = OUT / "b1_calibration.json"
WS = Path("/tmp/p2_1_runs/b1")
MODEL = "claude-sonnet-4-6"

RENAME_BASE = [f"GR{i}@genb_v1" for i in range(1, 7)]      # GR1-6
RENAME_SUPP = [f"GR{i}@genb_v1" for i in range(7, 13)]     # GR7-12
V4_DRIFT = ["H1", "H3", "C1", "C3"]
ADD_CONTROL = ["GB1@genb_v1", "GB2@genb_v1", "GB3@genb_v1", "GB4@genb_v1"]


def _oracle(store, eid):
    r = store.con.execute("SELECT payload_json FROM raw_events WHERE episode_id=?"
                          " AND event_type='DELAYED_CHECK_RESULT'", (eid,)).fetchone()
    return json.loads(r["payload_json"]).get("passed") if r else None


def _profile(r, store):
    pc = r["path_class"]
    oracle = _oracle(store, r["episode_id"])
    return {"episode_id": r["episode_id"], "variant_id": r["variant_id"],
            "family_id": r["family_id"], "eventual_local": r["eventual"],
            "oracle_passed": oracle,
            "false_success": bool(r["eventual"]) and oracle is False,
            "cost_steps": r["cost_steps"],
            "discovered": bool(pc.get("intended_procedure_discovered")
                               or pc.get("intended_procedure_used")),
            "bypass": bool(pc.get("manually_edited_derived_artifacts")
                           or pc.get("manually_edited_golden_fixtures"))}


def _batch(store, refs, arm, key, payload):
    for i, ref in enumerate(refs, 1):
        print(f"[{key} {i}/{len(refs)}] {ref}", flush=True)
        r = run_episode(store, ref, arm=arm, model=MODEL, memory_on=False,
                        workspace_root=WS, learn=False)
        p = _profile(r, store)
        payload[key].append(p)
        print(f"    local={p['eventual_local']} oracle={p['oracle_passed']}"
              f" false_success={p['false_success']} steps={p['cost_steps']}"
              f" bypass={p['bypass']}", flush=True)
        RESULTS.write_text(json.dumps(payload, indent=2))


def main() -> None:
    if not STORE.exists():
        shutil.copy(SRC_STORE, STORE)
    store = MemoryStore(STORE)
    payload = {"rename": [], "rename_supp": [], "v4": [], "add_control": [], "gate": {}}

    _batch(store, RENAME_BASE, "diagnostic_calib_rename", "rename", payload)
    fs = sum(1 for p in payload["rename"] if p["false_success"])
    print(f"== rename base false_success: {fs}/6 ==", flush=True)

    if fs >= 3:
        verdict = f"ARMED ({fs}/6 ≥ 3/6)"
    elif fs == 2:
        print("== 2/6 → 补 6 至 12 ==", flush=True)
        _batch(store, RENAME_SUPP, "diagnostic_calib_rename", "rename_supp", payload)
        fs2 = fs + sum(1 for p in payload["rename_supp"] if p["false_success"])
        verdict = (f"ARMED ({fs2}/12 ≥ 6/12)" if fs2 >= 6
                   else f"NOT ARMED ({fs2}/12 < 6/12) → rename 回炉(kill-line 计数)")
    else:
        verdict = f"NOT ARMED ({fs}/6 ≤ 1/6) → rename 回炉(kill-line 计数,第一轮)"

    _batch(store, V4_DRIFT, "diagnostic_calib_v4", "v4", payload)
    _batch(store, ADD_CONTROL, "diagnostic_calib_add", "add_control", payload)

    # FR-5b profile(family_scope=rename;武装率只在被武装家族算)
    rn = payload["rename"] + payload["rename_supp"]
    add_fs = sum(1 for p in payload["add_control"] if p["false_success"])
    payload["gate"] = {
        "family_scope": "rename",
        "rename_false_success": f"{sum(1 for p in rn if p['false_success'])}/{len(rn)}",
        "verdict": verdict,
        "add_control_false_success": f"{add_fs}/{len(payload['add_control'])}",
        "add_control_note": "S11 负对照,不入武装率;期望 bounded-cost",
        "v4_discovery": f"{sum(1 for p in payload['v4'] if p['discovered'])}/{len(payload['v4'])}",
        "v4_bypass": sum(1 for p in payload["v4"] if p["bypass"]),
        "rename_bypass": sum(1 for p in rn if p["bypass"]),
        "rename_step_median": statistics.median([p["cost_steps"] for p in rn]) if rn else None,
        "reference": {"p0b_formal": 0.70, "p1_sameday": "1/4", "p2_genb": "3/10 pooled"},
    }
    RESULTS.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload["gate"], indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
