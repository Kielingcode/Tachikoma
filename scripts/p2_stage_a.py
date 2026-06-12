"""P2 Stage A-2/A-4 — 重键重放(FR-14b)+ FR-25b shadow(0 run)。

A-2:四个 P1 store 副本全量 relearn(extractor/normalizer 已升 v3)。断言:
  - `mem_b80b9950`(echo EXIT 残留)证据并入 refresh key(deprecated 行),
    残留行被 orphan 清理;
  - 合并证据为 pre-death(流序在死亡点前)→ **不触发复活**;
  - 其余 memory 状态零漂移;重键审计表落盘。
A-4:对 wasteful store + near-miss 配对数据离线跑 FR-25b shadow——
  **只产 low_utility_candidate / inconclusive,不动 status**(G1' 裁决)。
副本演练:正本只在断言全过后由人工决定是否替换(本脚本不动正本)。
"""

import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tachicoma.store import MemoryStore

ROOT = Path(__file__).resolve().parent.parent
P1 = ROOT / "spikes" / "p1"
OUT = ROOT / "spikes" / "p2"
OUT.mkdir(parents=True, exist_ok=True)
RESULTS = OUT / "stage_a_rekey_shadow.json"

STORES = {"verified": P1 / "verified.sqlite", "wasteful": P1 / "wasteful.sqlite",
          "noise": P1 / "noise.sqlite", "u_arm": P1 / "u_arm.sqlite"}
STEP_GATE = 6   # P1 锁定噪声门(Stage B1 重校前的 shadow 参考值)


def rekey_replay(name: str, src: Path, payload: dict) -> None:
    tmp = Path(tempfile.mkdtemp()) / f"{name}.sqlite"
    shutil.copy(src, tmp)
    s = MemoryStore(tmp)
    before = {r["canonical_key"]: r["status"] for r in
              s.con.execute("SELECT canonical_key, status FROM memory_items")}
    for r in s.con.execute(
            "SELECT episode_id FROM episodes ORDER BY started_at").fetchall():
        s.relearn(r["episode_id"])
    after = {r["canonical_key"]: r["status"] for r in
             s.con.execute("SELECT canonical_key, status FROM memory_items")}
    removed = sorted(set(before) - set(after))
    added = sorted(set(after) - set(before))
    drifted = {k: (before[k], after[k]) for k in set(before) & set(after)
               if before[k] != after[k]}
    payload["rekey"][name] = {
        "keys_removed": removed, "keys_added": added, "status_drift": drifted,
        "replay_copy": str(tmp)}
    print(f"[{name}] removed={removed} added={added} drift={drifted}", flush=True)


def fr25b_shadow(payload: dict) -> None:
    """shadow 判定:wasteful store 的 refresh(被采纳)× near-miss 配对 delta。"""
    con = sqlite3.connect(f"file:{P1 / 'wasteful.sqlite'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT memory_id, status FROM memory_items"
                      " WHERE canonical_key LIKE '%refresh%'").fetchone()
    adoption = json.loads(con.execute(
        "SELECT per_context_json FROM belief_states WHERE memory_id=?",
        (row["memory_id"],)).fetchone()["per_context_json"]).get("adoption_support", 0)
    # 配对数据:P1 near-miss canary(verified store 中 arm=canary_nm*,shim 世界)
    vcon = sqlite3.connect(f"file:{P1 / 'verified.sqlite'}?mode=ro", uri=True)
    vcon.row_factory = sqlite3.Row
    pairs: dict[str, dict] = {}
    for r in vcon.execute("SELECT arm, cost_steps FROM episodes"
                          " WHERE arm LIKE 'canary_nm%'"):
        side, pid = r["arm"].rsplit("#p", 1)
        pairs.setdefault(pid, {})[side.replace("canary_nm_", "")] = r["cost_steps"]
    deltas = sorted(p["without"] - p["with"] for p in pairs.values()
                    if "with" in p and "without" in p)
    n = len(deltas)
    median = deltas[n // 2] if n % 2 else (deltas[n // 2 - 1] + deltas[n // 2]) / 2
    verdict = ("low_utility_candidate"
               if (adoption >= 3 and n >= 2 and median <= STEP_GATE) else "inconclusive")
    payload["fr25b_shadow"] = {
        "memory_id": row["memory_id"], "status_unchanged": row["status"],
        "adoption_support": adoption, "paired_deltas": deltas,
        "median_delta": median, "step_gate": STEP_GATE,
        "verdict": verdict,
        "note": "shadow 不动 status;low_utility 只能由 Stage D utility canary 确认"}
    print(f"[shadow] adoption={adoption} deltas={deltas} median={median}"
          f" → {verdict}", flush=True)


def main() -> None:
    payload = {"rekey": {}, "asserts": {}}
    for name, src in STORES.items():
        rekey_replay(name, src, payload)

    v = payload["rekey"]["verified"]
    # 残留 key 并入:echo-EXIT key 消失,refresh key 仍在且保持 deprecated
    payload["asserts"]["echo_exit_key_removed"] = any(
        "echo" in k for k in v["keys_removed"])
    payload["asserts"]["no_unexpected_drift"] = all(
        not r["status_drift"] for r in payload["rekey"].values())
    payload["asserts"]["refresh_stays_deprecated_no_false_revival"] = not any(
        "refresh" in k for k in v["status_drift"])

    fr25b_shadow(payload)
    RESULTS.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload["asserts"], indent=1))


if __name__ == "__main__":
    main()
