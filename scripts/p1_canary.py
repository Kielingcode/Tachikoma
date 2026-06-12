"""P1 Stage 2 — canary 配对升级批(FR-27/28,S9/S11)。

阈值已于跑批前锁定(P1_LOG):step_delta_gate=6,θ_adopt=0.8,near-miss 零容忍。
序贯规则:Round 1 = 6 对;逐对 delta>6 计数 6/6 过 / 4–5/6 补 3 对 / ≤3/6 停。
Near-miss 2 对 @shim_v1;2/2 干净不补,否则扩 NM3/NM4。
Store 血统:arm_b 副本 → verified.sqlite(Stage 3 在此 store 上继续)。
"""

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tachicoma.canary import apply_verdict, evaluate, run_canary_pairs
from tachicoma.store import MemoryStore

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "spikes" / "p1"
SRC_STORE = ROOT / "spikes" / "p0b" / "demo" / "arm_b.sqlite"
STORE = OUT_DIR / "verified.sqlite"
RESULTS = OUT_DIR / "canary_results.json"
WS = Path("/tmp/p1_runs/canary")

MODEL = "claude-sonnet-4-6"
GATE, THETA = 6, 0.8                       # P1_LOG 锁定,禁止改动
ROUND1 = ["H1", "H2", "H3", "C1", "C2", "C3"]
COND = ["C4", "C5", "H4"]
NM = ["NM1@shim_v1", "NM2@shim_v1"]
NM_EXPAND = ["NM3@shim_v1", "NM4@shim_v1"]


def _save(payload: dict) -> None:
    RESULTS.write_text(json.dumps(payload, indent=2))


def _pair_summary(pairs: list[dict]) -> list[dict]:
    out = []
    for p in pairs:
        w, wo = p["with"], p["without"]
        out.append({"pair": p["pair"], "variant": p["variant"],
                    "delta": wo["cost_steps"] - w["cost_steps"],
                    "with_steps": w["cost_steps"], "without_steps": wo["cost_steps"],
                    "with_eventual": w["eventual"], "without_eventual": wo["eventual"],
                    "with_injected": w["injected"]})
    return out


def main() -> None:
    if not STORE.exists():
        shutil.copy(SRC_STORE, STORE)
    store = MemoryStore(STORE)
    mid = store.con.execute(
        "SELECT memory_id FROM memory_items WHERE canonical_key LIKE '%refresh%'"
        " AND status LIKE 'active%'").fetchone()["memory_id"]
    print(f"memory under test: {mid}", flush=True)
    payload = {"memory_id": mid, "gate": GATE, "theta_adopt": THETA,
               "rounds": [], "verdict": None}

    print("== Round 1: 6 pairs ==", flush=True)
    r1 = run_canary_pairs(store, ROUND1, MODEL, WS)
    s1 = _pair_summary(r1)
    payload["rounds"].append({"name": "round1", "pairs": s1})
    _save(payload)
    n_pass = sum(1 for p in s1 if p["delta"] > GATE)
    print(f"round1 per-pair deltas: {[p['delta'] for p in s1]} → {n_pass}/6 over gate",
          flush=True)

    if n_pass <= 3:
        payload["sequential_decision"] = f"STOP: {n_pass}/6 ≤ 3 — 回去修设计"
        _save(payload)
        print(payload["sequential_decision"])
        return
    if n_pass < 6:
        print(f"== Conditional round: +3 pairs ({n_pass}/6) ==", flush=True)
        rc = run_canary_pairs(store, COND, MODEL, WS, pair_offset=len(ROUND1))
        sc = _pair_summary(rc)
        payload["rounds"].append({"name": "conditional", "pairs": sc})
        _save(payload)
        payload["sequential_decision"] = f"{n_pass}/6 → expanded by 3"
    else:
        payload["sequential_decision"] = "6/6 → direct"

    print("== Near-miss: 2 pairs @shim_v1 ==", flush=True)
    rn = run_canary_pairs(store, NM, MODEL, WS, pair_offset=100, nearmiss=True)
    sn = _pair_summary(rn)
    payload["rounds"].append({"name": "nearmiss", "pairs": sn})
    _save(payload)
    nm_dirty = any(p["without_eventual"] and not p["with_eventual"] for p in sn)
    if nm_dirty:
        print("== Near-miss dirty → expand 2 pairs ==", flush=True)
        rn2 = run_canary_pairs(store, NM_EXPAND, MODEL, WS, pair_offset=102, nearmiss=True)
        payload["rounds"].append({"name": "nearmiss_expand", "pairs": _pair_summary(rn2)})
        _save(payload)

    verdict = evaluate(store, mid, step_delta_gate=GATE, theta_adopt=THETA,
                       nearmiss_max_neg_flips=0)
    applied = apply_verdict(store, verdict)
    payload["verdict"] = verdict
    payload["applied"] = applied
    _save(payload)
    print(json.dumps(verdict, indent=1))
    print(f"apply_verdict → {applied}")


if __name__ == "__main__":
    main()
