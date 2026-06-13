"""P2.2 Stage C — S15 organic 出生(genb_hs,memory_off + feedback_on)。

前置:Stage B delivery PASS + genb_hs 武装 sanity 全过。
每 run 结构上必先 oracle-fail(migrate 不排序)→ 瓶颈 = "agent 发现 check 并修"
(C 首测的不可约风险)。**逐 run 分环诊断**(① 未探索 ② 探索未运行 check
③ 运行见 fail 未修 ④ 修了 oracle 仍红 ⑤ 修了 oracle 绿 = VP 出生)。
kill-line:跑 6;≥2 变体 ⑤ → 晋升 active + S15 组合 + S10-organic;
0/6 按卡点分布(多数③→P3 不可达;多数①/②→上车点回炉;④→fixture 回炉)。
反馈文案迭代上界 ≤ 2 版(L1/L2),不在临界面无界试探。
"""

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tachicoma.resolver import check_segments, normalize_command
from tachicoma.runner import run_episode
from tachicoma.store import MemoryStore

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "spikes" / "p2_2"
STORE = OUT / "stage_c.sqlite"
RESULTS = OUT / "stage_c.json"
WS = Path("/tmp/p2_2_runs/c")
MODEL = "claude-sonnet-4-6"
CHECK_CMD = "python3 tools/check_contract.py"

ARMED = ["GR2@genb_hs", "GR4@genb_hs", "GR5@genb_hs", "GR6@genb_hs",
         "GR7@genb_hs", "GR9@genb_hs"]   # 6 武装 rename 变体


def _seed(store):
    eid = f"seed-{uuid.uuid4().hex[:6]}"
    store.ingest_episode({
        "episode_id": eid, "task_id": "seed", "family_id": "rename-field",
        "generator_template": "genb_hs", "arm": "diagnostic_seed", "repo": "orderkit",
        "model_version": "seed", "agent_version": "seed",
        "started_at": "2020-01-01T00:00:00", "ended_at": "2020-01-01T00:00:00",
        "first_try_success": 0, "eventual_success": 1, "cost_steps": 0,
        "cost_tokens": 0, "wrong_turn_count": 0,
    }, [{"step_idx": 1, "event_type": "DELAYED_CHECK_RESULT",
         "payload": {"passed": False, "source": "seeded", "seeded": True}}])


def _classify(store, eid):
    """从 raw_events 分环 ①-⑤。返回 (stage, signals)。"""
    ep, _, _ = store.episode_view(eid)
    check_runs = [a for a in ep.actions
                  if a.kind in ("run", "test_run") and a.command
                  and CHECK_CMD in check_segments(a.command)]
    read_tools = any(a.kind == "read" and (a.path or "").startswith("tools/")
                     for a in ep.actions)
    ran_check = len(check_runs) > 0
    # check 自身 fail→pass 翻转
    seen_fail = False
    check_flip = False
    for a in check_runs:
        if a.test_passed is False:
            seen_fail = True
        elif a.test_passed and seen_fail:
            check_flip = True
    saw_check_fail = any(a.test_passed is False for a in check_runs)
    oracle = next((a.test_passed for a in ep.actions if a.kind == "oracle_check"), None)
    vp = store.con.execute(
        "SELECT COUNT(*) c FROM claims WHERE episode_id=? AND claim_type='ValidationParity'"
        " AND polarity>0", (eid,)).fetchone()["c"]
    sig = {"ran_check": ran_check, "read_tools": read_tools, "saw_check_fail": saw_check_fail,
           "check_flip": check_flip, "oracle": oracle, "vp_minted": vp}
    if vp >= 1 and oracle:
        stage = "5_vp_born"
    elif ran_check and not saw_check_fail and oracle:
        # ⑥ 跑了 check、一次做对(无 fail→无 flip)、oracle 绿 = agent 能力/世界没逼出错;
        # 合法 not-born。**必须先于 ① 判**,否则误并进"从没探索到 check"(上车点失灵),
        # 污染 kill-line 的眼睛(①回炉上车点 vs ⑥agent能力 vs ③转P3 的区分)。
        stage = "6_correct_first_try"
    elif oracle is False and ran_check:
        stage = "4_fixed_oracle_still_red"
    elif ran_check and saw_check_fail and not check_flip:
        stage = "3_ran_saw_fail_didnt_fix"
    elif read_tools and not ran_check:
        stage = "2_explored_not_run"
    else:
        stage = "1_not_explored"
    return stage, sig


def main():
    store = MemoryStore(STORE)
    _seed(store)
    payload = {"runs": [], "stage_dist": {}, "verdict": {}}

    for i, ref in enumerate(ARMED, 1):
        print(f"[C {i}/6] {ref}", flush=True)
        r = run_episode(store, ref, arm="genb_hs_vp", model=MODEL, memory_on=False,
                        workspace_root=WS, learn=True, feedback_level=2)
        stage, sig = _classify(store, r["episode_id"])
        payload["runs"].append({"variant": ref, "stage": stage, **sig,
                                "cost_steps": r["cost_steps"]})
        print(f"    stage={stage} ran_check={sig['ran_check']} flip={sig['check_flip']}"
              f" oracle={sig['oracle']} vp_minted={sig['vp_minted']}", flush=True)
        RESULTS.write_text(json.dumps(payload, indent=2))

    from collections import Counter
    dist = Counter(r["stage"] for r in payload["runs"])
    payload["stage_dist"] = dict(dist)
    births = dist.get("5_vp_born", 0)
    vp_variants = len({r["variant"] for r in payload["runs"] if r["stage"] == "5_vp_born"})

    if vp_variants >= 2:
        verdict = f"S15 organic VP 出生:{vp_variants} 变体 ⑤ → 晋升,进组合/S10-organic"
    else:
        # 0/6(或 <2)按卡点分布定结论
        top = dist.most_common(1)[0][0] if dist else "none"
        if top.startswith("3_"):
            verdict = "0/6:多数③(探索了不肯修)→ S15 NOT MEASURABLE,agent class 不肯先错后修 → P3"
        elif top.startswith(("1_", "2_")):
            verdict = ("0/6:多数①/②(没探索到/没运行 check)→ 上车点在 genb_hs 失灵 → "
                       "回炉上车点【文案 ≤ 2 版上界】,不转 P3")
        elif top.startswith("4_"):
            verdict = "0/6:出现④(修了 oracle 仍红)→ genb_hs 修复路径 bug → 回炉 fixture"
        else:
            verdict = f"0/6:卡点分布 {dict(dist)},需人工裁定"
    payload["verdict"] = {"vp_organic_variants": vp_variants, "births": births,
                          "stage_dist": dict(dist), "decision": verdict}
    vp = store.con.execute("SELECT memory_id, status FROM memory_items"
                           " WHERE memory_type='ValidationParity'").fetchone()
    payload["vp_memory"] = dict(vp) if vp else None
    RESULTS.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload["verdict"], indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
