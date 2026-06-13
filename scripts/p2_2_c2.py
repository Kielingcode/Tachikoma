"""P2.2 Stage C(续)— 第二家族(add-field)→ VP 晋升 → S15 组合 + S10-organic。

C1 实测:2 个 rename VP 出生但停 candidate(都属 rename-field 单家族,gate fam≥2 拦)。
本续:在 genb_hs 跑 add-field 变体(不同 task family,同 VP canonical_key),
使 VP organic 晋升 active(多家族支撑——家族分层门的正确语义:验证习惯需跨任务
类型见效才可信)。VP active 后:S15 组合 smoke(PD+VP co-注入)+ S10-organic。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tachicoma.path_classifier import adoption_record
from tachicoma.resolver import check_segments
from tachicoma.runner import run_episode
from tachicoma.store import MemoryStore

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "spikes" / "p2_2"
STORE = OUT / "stage_c.sqlite"          # 续用 C1 store(已有 2 个 rename VP candidate)
RESULTS = OUT / "stage_c2.json"
WS = Path("/tmp/p2_2_runs/c2")
MODEL = "claude-sonnet-4-6"
CHECK_CMD = "python3 tools/check_contract.py"
MIGRATE_CMD = "python3 tools/migrate.py"

ADD_FAMILY = ["GB1@genb_hs", "GB2@genb_hs", "GB3@genb_hs", "GB4@genb_hs"]
HELDOUT = ["GH1@genb_hs", "GH2@genb_hs"]   # S10-organic / S15 组合用


def _oracle(store, eid):
    r = store.con.execute("SELECT payload_json FROM raw_events WHERE episode_id=?"
                          " AND event_type='DELAYED_CHECK_RESULT'"
                          " AND json_extract(payload_json,'$.source')='harness_hidden_oracle'",
                          (eid,)).fetchone()
    return json.loads(r["payload_json"]).get("passed") if r else None


def _vp(store):
    return store.con.execute("SELECT memory_id, status FROM memory_items"
                             " WHERE memory_type='ValidationParity'").fetchone()


def main():
    store = MemoryStore(STORE)
    payload = {"add_family": [], "s15": [], "asserts": {}}

    # --- 第二家族:add-field VP 出生 → 晋升 ---
    for i, ref in enumerate(ADD_FAMILY, 1):
        vp = _vp(store)
        if vp and vp["status"].startswith("active"):
            break
        print(f"[add {i}/{len(ADD_FAMILY)}] {ref}", flush=True)
        r = run_episode(store, ref, arm="genb_hs_vp", model=MODEL, memory_on=False,
                        workspace_root=WS, learn=True, feedback_level=2)
        eid = r["episode_id"]
        ep, _, _ = store.episode_view(eid)
        vp_born = store.con.execute(
            "SELECT COUNT(*) c FROM claims WHERE episode_id=? AND claim_type='ValidationParity'"
            " AND polarity>0", (eid,)).fetchone()["c"]
        vp = _vp(store)
        payload["add_family"].append({"variant": ref, "oracle": _oracle(store, eid),
                                      "vp_born": vp_born, "vp_status": vp["status"] if vp else None})
        print(f"    oracle={_oracle(store, eid)} vp_born={vp_born}"
              f" vp_status={vp['status'] if vp else None}", flush=True)
        RESULTS.write_text(json.dumps(payload, indent=2))

    vp = _vp(store)
    payload["asserts"]["vp_organic_promoted"] = bool(vp and vp["status"].startswith("active"))
    vp_fams = store.con.execute(
        "SELECT COUNT(DISTINCT ep.family_id) c FROM claims c2"
        " JOIN episodes ep ON c2.episode_id=ep.episode_id"
        " JOIN evidence_links e ON c2.claim_id=e.claim_id"
        " WHERE c2.claim_type='ValidationParity' AND c2.polarity>0"
        " AND e.evidence_source='organic_task'").fetchone()["c"]
    payload["asserts"]["vp_distinct_families"] = vp_fams
    RESULTS.write_text(json.dumps(payload, indent=2))
    print(f"VP: status={vp['status'] if vp else None}, distinct organic families={vp_fams}",
          flush=True)

    if not payload["asserts"]["vp_organic_promoted"]:
        print("VP 未晋升——记录(可能需更多家族);S15 组合待 active VP", flush=True)
        return

    # --- S15 组合 smoke(active VP + PD co-注入)---
    mig = store.con.execute("SELECT memory_id FROM memory_items WHERE canonical_key"
                            " LIKE '%migrate%' AND status LIKE 'active%'").fetchone()
    vp_mid = vp["memory_id"]
    for ref in HELDOUT:
        print(f"[s15 {ref}]", flush=True)
        r = run_episode(store, ref, arm="s15_combo", model=MODEL, memory_on=True,
                        workspace_root=WS, learn=False, k=3, feedback_level=2)
        eid = r["episode_id"]
        ep, _, _ = store.episode_view(eid)
        vp_ad = any(a.kind in ("run", "test_run") and a.command
                    and CHECK_CMD in check_segments(a.command) for a in ep.actions)
        pd_ad = adoption_record(ep, "src/models.py", MIGRATE_CMD).adopted
        payload["s15"].append({"variant": ref, "injected": r["injected"],
                               "vp_adopted": vp_ad, "pd_adopted": pd_ad,
                               "oracle": _oracle(store, eid)})
        print(f"    injected={r['injected']} vp_adopted={vp_ad} pd_adopted={pd_ad}", flush=True)
        RESULTS.write_text(json.dumps(payload, indent=2))

    both = [h for h in payload["s15"] if len(h["injected"]) >= 2]
    payload["asserts"]["s15"] = {
        "runs_with_both_types": len(both),
        "dual_adoption": sum(1 for h in both if h["vp_adopted"] and h["pd_adopted"]),
        "vp_active_injected": all(vp_mid in h["injected"] for h in payload["s15"]),
        "mig": mig["memory_id"] if mig else None,
    }
    RESULTS.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload["asserts"], indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
