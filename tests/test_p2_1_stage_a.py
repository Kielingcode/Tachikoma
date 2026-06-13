"""P2.1 Stage A 单测:FR-9b case ⓪、FR-8b 反馈通道、canary 命名空间隔离、
fixture 版本标记。"""

import json

from tachicoma.canary import batch_episode_ids, evaluate
from tachicoma.feedback import (DEFAULT_LEVEL, FEEDBACK_TEXT, build_feedback,
                                last_family_oracle_fail)
from tachicoma.store import MemoryStore

PYTEST = "python3 -m pytest tests/ -q"
CHECK = "python3 tools/check_contract.py"


def _meta(eid, family, arm="memory_off", started="t0", success=1, repo="orderkit"):
    return {"episode_id": eid, "task_id": f"t_{eid}", "family_id": family,
            "generator_template": "hidden_coupling_v4", "arm": arm, "repo": repo,
            "model_version": "test", "agent_version": "test",
            "started_at": started, "ended_at": started, "first_try_success": 0,
            "eventual_success": success, "cost_steps": 6, "cost_tokens": 100,
            "wrong_turn_count": 0}


# --------------------------------------------- FR-9b case ⓪ ----

def test_case0_oracle_fail_suppresses_organic_positive():
    """oracle 存在且 fail → 同 episode 不铸 organic 正向(PD);采纳负向仍 fire。"""
    s = MemoryStore()
    # organic PD 轨迹(edit→refresh→本地 pass)但 oracle fail = 假成功
    ev = [
        {"step_idx": 1, "event_type": "TEST_RUN", "payload": {"command": PYTEST, "passed": False}},
        {"step_idx": 2, "event_type": "FILE_EDIT", "payload": {"path": "src/models.py"}},
        {"step_idx": 3, "event_type": "COMMAND_RUN", "payload": {"command": "python3 tools/refresh.py"}},
        {"step_idx": 4, "event_type": "TEST_RUN", "payload": {"command": PYTEST, "passed": True}},
        {"step_idx": 5, "event_type": "DELAYED_CHECK_RESULT", "payload": {"passed": False}},
    ]
    s.ingest_episode(_meta("e1", "add-field", started="t1"), ev)
    s.relearn("e1")
    # case ⓪:无 organic 正向铸出
    assert s.con.execute("SELECT COUNT(*) c FROM claims WHERE polarity>0").fetchone()["c"] == 0
    aud = s.con.execute("SELECT COUNT(*) c FROM audit_log WHERE action="
                        "'case0_organic_positive_suppressed'").fetchone()["c"]
    assert aud == 1
    # 对照:oracle pass 时正常铸 organic 正向
    ev2 = [e for e in ev[:-1]]
    ev2.append({"step_idx": 5, "event_type": "DELAYED_CHECK_RESULT", "payload": {"passed": True}})
    s.ingest_episode(_meta("e2", "add-field", started="t2"), ev2)
    s.relearn("e2")
    assert s.con.execute("SELECT COUNT(*) c FROM claims WHERE polarity>0").fetchone()["c"] == 1


# ------------------------------------------- FR-8b 反馈通道 ----

def _oracle_episode(s, eid, family, started, oracle_passed, repo="billing"):
    ev = [{"step_idx": 1, "event_type": "TEST_RUN", "payload": {"command": PYTEST, "passed": True}},
          {"step_idx": 2, "event_type": "DELAYED_CHECK_RESULT", "payload": {"passed": oracle_passed}}]
    s.ingest_episode(_meta(eid, family, started=started, repo=repo), ev)


def test_fr8b_feedback_built_only_on_prior_family_oracle_fail():
    s = MemoryStore()
    _oracle_episode(s, "g1", "rename", "t1", oracle_passed=False)
    fb = build_feedback(s, "billing", "rename", before="t5", level=DEFAULT_LEVEL)
    assert fb is not None
    assert fb["text"] == FEEDBACK_TEXT[2]
    assert fb["feedback_source_episode_id"] == "g1"
    assert fb["feedback_family_scope"] == "rename"
    assert fb["feedback_oracle_type"] == "wire_compatibility"
    assert len(fb["feedback_text_hash"]) == 16
    # 不泄漏工具名/修法
    assert "check_contract" not in fb["text"] and "declare_done" not in fb["text"]
    assert "契约检查" not in fb["text"]
    # 无前序失败 → None
    assert build_feedback(s, "billing", "add-field", before="t5") is None
    # 跨 repo 不串
    assert build_feedback(s, "orderkit", "rename", before="t5") is None


def test_fr8b_is_pure_function_of_raw_events():
    """同一 raw_events → 同一反馈(纯函数,不依赖可变状态);时间窗口正确。"""
    s = MemoryStore()
    _oracle_episode(s, "g1", "rename", "t1", oracle_passed=False)
    a = build_feedback(s, "billing", "rename", before="t5")
    b = build_feedback(s, "billing", "rename", before="t5")
    assert a == b
    # before 早于失败 episode → 不取(时间因果)
    assert build_feedback(s, "billing", "rename", before="t0") is None


def test_fr8b_picks_most_recent_fail():
    s = MemoryStore()
    _oracle_episode(s, "g1", "rename", "t1", oracle_passed=False)
    _oracle_episode(s, "g2", "rename", "t2", oracle_passed=True)
    _oracle_episode(s, "g3", "rename", "t3", oracle_passed=False)
    hit = last_family_oracle_fail(s, "billing", "rename", before="t5")
    assert hit[0] == "g3"


# ------------------------------ canary 命名空间隔离 ----

def test_canary_evaluate_respects_explicit_episode_scope():
    """继承快照场景:store 有旧批 canary(pair#p0/#p1),新批也用 #p0/#p1;
    episode_ids 限定只聚合新批,旧批不污染。"""
    s = MemoryStore()
    mid = "mem_x"
    s.con.execute("INSERT INTO memory_items (memory_id, memory_type, canonical_key,"
                  " trigger_json, action_json, rival_key, scope_json, status, causal_verified)"
                  " VALUES (?,?,?,?,?,?,?,?,0)",
                  (mid, "ProceduralDependency", "k", json.dumps({"after_edit": "src/models.py"}),
                   json.dumps({"must_run": "python3 tools/refresh.py"}), "rk",
                   json.dumps({"repo": "orderkit"}), "active_correlational"))
    s.con.commit()

    def pair(eid_w, eid_wo, pid, w_steps, wo_steps, started):
        for eid, arm, steps in ((eid_w, f"canary_with#p{pid}", w_steps),
                                 (eid_wo, f"canary_without#p{pid}", wo_steps)):
            m = _meta(eid, "add-field", arm=arm, started=started)
            m["cost_steps"] = steps
            s.ingest_episode(m, [{"step_idx": 0, "event_type": "MEMORY_INJECTED",
                                  "payload": {"memory_ids": [mid]}}] if "with" in arm else [])

    # 旧批(污染源,继承快照携带):用不同 pid(5/6,如 P1 的 p0-8 vs 新 p0-2),
    # 不会被新批覆盖 → 全店扫描会多聚合这些 pair(Stage D 实测 n_pairs 虚高)
    pair("old_w5", "old_wo5", 5, 1, 99, "t1")
    pair("old_w6", "old_wo6", 6, 1, 99, "t2")
    # 新批:小 delta(真实低效用)
    new_ids = set()
    for i, (ws, wos) in enumerate([(8, 10), (8, 11)]):
        pair(f"new_w{i}", f"new_wo{i}", i, ws, wos, f"t{3+i}")
        new_ids |= {f"new_w{i}", f"new_wo{i}"}

    v_all = evaluate(s, mid, step_delta_gate=6, theta_adopt=0.8)
    v_scoped = evaluate(s, mid, step_delta_gate=6, theta_adopt=0.8, episode_ids=new_ids)
    assert v_all["n_pairs"] == 4 and v_all["median_step_delta"] > 50   # 旧批污染聚合
    assert v_scoped["median_step_delta"] == 2.5     # 隔离后真实小 delta(median [2,3])
    assert v_scoped["n_pairs"] == 2


def test_batch_episode_ids_helper():
    pairs = [{"with": {"episode_id": "a"}, "without": {"episode_id": "b"}},
             {"with": {"episode_id": "c"}, "without": None}]
    assert batch_episode_ids(pairs) == {"a", "b", "c"}


# ------------------------------ fixture 版本标记 ----

def test_fixture_version_detects_content_change():
    import tempfile
    from pathlib import Path
    from tachicoma.feedback import write_fixture_version
    d = Path(tempfile.mkdtemp())
    (d / "a.txt").write_text("x")
    h1 = write_fixture_version(d, "genb_v1", "rev3")
    assert "key=genb_v1" in (d / "fixture_version.txt").read_text()
    (d / "a.txt").write_text("y")
    h2 = write_fixture_version(d, "genb_v1", "rev3")
    assert h1 != h2   # 改内容没改键名 → 哈希变,可检测
