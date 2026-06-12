"""P1 Stage 3 — Anti-rot 三段(S7/S8/S13)。

血统:Stage 2 的 verified.sqlite(verified 也必须死得掉,G-3)。
  3a/3b 主线:害人(rot_v2a)→ 杀旧立新(rot_v2b),同一 store 续跑;
  3c wasteful:**3a 之前的快照**(wasteful.sqlite)——否则 refresh 已 disputed,
  shim 上无注入可言。
早停:3a 过线(adopted-but-did-not-fix ≥2 ∧ 旧 memory disputed)即停;
  3b 过线(migrate top-1 注入 ∧ 旧 memory deprecated)即停;各段前 2 run 兼任 smoke。
"""

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tachicoma.runner import run_episode
from tachicoma.store import MemoryStore

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "spikes" / "p1"
VERIFIED = OUT_DIR / "verified.sqlite"
WASTEFUL = OUT_DIR / "wasteful.sqlite"
RESULTS = OUT_DIR / "rot_results.json"
WS = Path("/tmp/p1_runs/rot")
MODEL = "claude-sonnet-4-6"

SEG_3A = ["R1@rot_v2a", "R2@rot_v2a", "R3@rot_v2a", "R4@rot_v2a"]
# 3b 前两个 run 跨 family(add+rename),晋升门 fam≥2 最快两 run 可满足
SEG_3B = ["R1@rot_v2b", "R3@rot_v2b", "R2@rot_v2b", "R4@rot_v2b",
          "C4@rot_v2b", "C5@rot_v2b"]
SEG_3C = ["W1@shim_v1", "W2@shim_v1", "W3@shim_v1"]


def _refresh_mid(store):
    return store.con.execute(
        "SELECT memory_id, status FROM memory_items"
        " WHERE canonical_key LIKE '%refresh%'").fetchone()


def _migrate_row(store):
    return store.con.execute(
        "SELECT memory_id, status FROM memory_items"
        " WHERE canonical_key LIKE '%migrate%'").fetchone()


def _status(store, mid):
    r = store.con.execute(
        "SELECT status FROM memory_items WHERE memory_id=?", (mid,)).fetchone()
    return r["status"] if r else None


def _adoption_negs(store, mid):
    return store.con.execute(
        "SELECT COUNT(*) c FROM evidence_links WHERE memory_id=? AND polarity<0"
        " AND evidence_source='adoption_outcome'", (mid,)).fetchone()["c"]


def _slim(r):
    return {k: r[k] for k in ("episode_id", "variant_id", "arm", "injected",
                              "first_try", "eventual", "cost_steps", "path_class")}


def main() -> None:
    payload = {"3a": [], "3b": [], "3c": [], "asserts": {}}

    def save():
        RESULTS.write_text(json.dumps(payload, indent=2))

    # ---- 快照(3c 用 3a 之前的状态)----
    if not WASTEFUL.exists():
        shutil.copy(VERIFIED, WASTEFUL)

    store = MemoryStore(VERIFIED)
    old = _refresh_mid(store)
    mid = old["memory_id"]
    payload["old_memory"] = {"memory_id": mid, "status_pre_3a": old["status"]}
    print(f"old memory {mid} status={old['status']}", flush=True)

    # ---- 3a: stale exposure(rot_v2a)----
    for i, ref in enumerate(SEG_3A, 1):
        print(f"[3a {i}/{len(SEG_3A)}] {ref}", flush=True)
        r = run_episode(store, ref, arm="rot3a", model=MODEL, memory_on=True,
                        workspace_root=WS / "3a", learn=True)
        payload["3a"].append(_slim(r))
        negs, st = _adoption_negs(store, mid), _status(store, mid)
        print(f"    eventual={r['eventual']} injected={r['injected']}"
              f" adoption_negs={negs} status={st}", flush=True)
        save()
        if negs >= 2 and st == "disputed":
            print("3a 过线即停:adopted-but-did-not-fix ≥2 ∧ disputed", flush=True)
            break
    payload["asserts"]["3a_passline"] = (
        _adoption_negs(store, mid) >= 2 and _status(store, mid) == "disputed")
    payload["asserts"]["g3_verified_demoted"] = (
        payload["old_memory"]["status_pre_3a"] == "active_verified"
        and _status(store, mid) in ("disputed", "deprecated"))
    payload["old_memory"]["status_post_3a"] = _status(store, mid)
    save()

    # ---- 3b: rival recovery(rot_v2b)----
    for i, ref in enumerate(SEG_3B, 1):
        print(f"[3b {i}/{len(SEG_3B)}] {ref}", flush=True)
        r = run_episode(store, ref, arm="rot3b", model=MODEL, memory_on=True,
                        workspace_root=WS / "3b", learn=True)
        payload["3b"].append(_slim(r))
        mig = _migrate_row(store)
        old_st = _status(store, mid)
        mig_desc = f"{mig['memory_id']}:{mig['status']}" if mig else None
        print(f"    eventual={r['eventual']} injected={r['injected']}"
              f" migrate={mig_desc} old={old_st}", flush=True)
        save()
        # 过线:本 run 注入恰为 migrate(rival top-1 归位)∧ 旧 memory 观察期满
        if (mig and r["injected"] == [mig["memory_id"]] and old_st == "deprecated"
                and i >= 3):
            print("3b 过线即停:top-1=migrate ∧ 旧 memory deprecated", flush=True)
            break
    mig = _migrate_row(store)
    payload["rival_memory"] = ({"memory_id": mig["memory_id"], "status": mig["status"]}
                               if mig else None)
    payload["old_memory"]["status_post_3b"] = _status(store, mid)
    payload["asserts"]["3b_top1_migrate_only"] = any(
        mig and r["injected"] == [mig["memory_id"]] for r in payload["3b"])
    payload["asserts"]["3b_old_deprecated"] = _status(store, mid) == "deprecated"
    save()

    # ---- 3c: wasteful(shim,3a 前快照——refresh 仍 active)----
    wstore = MemoryStore(WASTEFUL)
    wmid = _refresh_mid(wstore)["memory_id"]
    for i, ref in enumerate(SEG_3C, 1):
        print(f"[3c {i}/{len(SEG_3C)}] {ref}", flush=True)
        r = run_episode(wstore, ref, arm="rot3c", model=MODEL, memory_on=True,
                        workspace_root=WS / "3c", learn=True)
        payload["3c"].append(_slim(r))
        adoption = wstore.con.execute(
            "SELECT per_context_json FROM belief_states WHERE memory_id=?",
            (wmid,)).fetchone()
        print(f"    eventual={r['eventual']} steps={r['cost_steps']}"
              f" injected={r['injected']}"
              f" belief={adoption['per_context_json'] if adoption else None}", flush=True)
        save()
    payload["asserts"]["3c_refresh_still_active"] = str(
        _status(wstore, wmid)).startswith("active")
    payload["3c_adoption_support"] = json.loads(
        wstore.con.execute("SELECT per_context_json FROM belief_states WHERE memory_id=?",
                           (wmid,)).fetchone()["per_context_json"])
    save()
    print(json.dumps(payload["asserts"], indent=1))


if __name__ == "__main__":
    main()
