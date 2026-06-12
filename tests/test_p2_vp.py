"""P2 Stage A-0:ValidationParity 提取机器(FR-9b)。

覆盖:VP 出生(check 自身翻转 + oracle 确认)/ 坍缩反例(VP 不得提成 PD 形状,
PD 不受 VP 干扰)/ oracle 归因 case ③④⑤ / 检索 always-on / 渲染分型。
"""

import json

from tachicoma.extractor import extract
from tachicoma.path_classifier import Episode
from tachicoma.resolver import canonical_key
from tachicoma.runner import events_to_actions
from tachicoma.store import MemoryStore

CHECK = "python3 tools/check_contract.py"
PYTEST = "python3 -m pytest tests/ -q"


def _meta(eid, family, arm="memory_off", started="t0", success=1):
    return {"episode_id": eid, "task_id": f"t_{eid}", "family_id": family,
            "generator_template": "hidden_coupling_v4", "arm": arm, "repo": "orderkit",
            "model_version": "test", "agent_version": "test",
            "started_at": started, "ended_at": started, "first_try_success": 0,
            "eventual_success": success, "cost_steps": 6, "cost_tokens": 100,
            "wrong_turn_count": 0}


def _vp_discovery_events():
    """organic VP 轨迹:本地绿 → check fail → 修 → check pass → oracle pass。"""
    return [
        {"step_idx": 1, "event_type": "TEST_RUN",
         "payload": {"command": PYTEST, "passed": False,
                     "source": "harness_pristine_check"}},
        {"step_idx": 2, "event_type": "FILE_EDIT", "payload": {"path": "src/models.py"}},
        {"step_idx": 3, "event_type": "TEST_RUN", "payload": {"command": PYTEST, "passed": True}},
        {"step_idx": 4, "event_type": "TEST_RUN",
         "payload": {"command": CHECK, "passed": False, "source": "check_tool"}},
        {"step_idx": 5, "event_type": "FILE_EDIT", "payload": {"path": "src/wire.py"}},
        {"step_idx": 6, "event_type": "TEST_RUN",
         "payload": {"command": CHECK, "passed": True, "source": "check_tool"}},
        {"step_idx": 7, "event_type": "DELAYED_CHECK_RESULT", "payload": {"passed": True}},
    ]


def _ep(events, **kw):
    return Episode(actions=events_to_actions(events), eventual_success=True,
                   cost_steps=7, cost_tokens=10, memory_injected=False, **kw)


def test_vp_claim_born_from_check_flip_plus_oracle():
    claims = extract(_ep(_vp_discovery_events()))
    vp = [c for c in claims if c.claim_type == "ValidationParity"]
    assert len(vp) == 1
    assert vp[0].trigger == {"before": "declare_done"}
    assert vp[0].action == {"must_run": CHECK}
    assert vp[0].polarity == 1
    assert canonical_key("ValidationParity", vp[0].trigger, vp[0].action) == \
        f"ValidationParity|declare_done|{CHECK}"


def test_vp_not_collapsed_to_pd_and_vice_versa():
    """反例(S15 前提):VP 轨迹不产 PD-shape 的 check claim;
    经典 PD 轨迹不产 VP claim。"""
    claims = extract(_ep(_vp_discovery_events()))
    pd = [c for c in claims if c.claim_type == "ProceduralDependency"]
    assert all(CHECK not in c.action.get("must_run", "") for c in pd)
    # 经典 PD 轨迹(无 check、无 oracle)
    pd_events = [
        {"step_idx": 1, "event_type": "TEST_RUN", "payload": {"command": PYTEST, "passed": False}},
        {"step_idx": 2, "event_type": "FILE_EDIT", "payload": {"path": "src/models.py"}},
        {"step_idx": 3, "event_type": "COMMAND_RUN",
         "payload": {"command": "python3 tools/refresh.py"}},
        {"step_idx": 4, "event_type": "TEST_RUN", "payload": {"command": PYTEST, "passed": True}},
    ]
    claims2 = extract(_ep(pd_events))
    assert all(c.claim_type == "ProceduralDependency" for c in claims2) and claims2


def test_vp_requires_oracle_confirmation():
    ev = [e for e in _vp_discovery_events() if e["event_type"] != "DELAYED_CHECK_RESULT"]
    assert not [c for c in extract(_ep(ev)) if c.claim_type == "ValidationParity"]
    ev2 = _vp_discovery_events()
    ev2[-1] = {"step_idx": 7, "event_type": "DELAYED_CHECK_RESULT", "payload": {"passed": False}}
    assert not [c for c in extract(_ep(ev2)) if c.claim_type == "ValidationParity"]


def _vp_store():
    """两 family organic VP 发现 → 晋升 active。"""
    s = MemoryStore()
    for i, fam in enumerate(("add-field", "rename-field")):
        s.ingest_episode(_meta(f"v{i}", fam, started=f"t{i}"), _vp_discovery_events())
        s.relearn(f"v{i}")
    return s


def _vp_mid(s):
    return s.con.execute("SELECT memory_id, status FROM memory_items"
                         " WHERE memory_type='ValidationParity'").fetchone()


def test_vp_promotion_and_retrieval_always_on_and_rendering():
    from pathlib import Path
    import tempfile
    from tachicoma.retrieval import render_payload, retrieve
    s = _vp_store()
    row = _vp_mid(s)
    assert row["status"] == "active_correlational"      # fam=2, s=2, f=0
    ws = Path(tempfile.mkdtemp())
    (ws / "src").mkdir()
    (ws / "src" / "models.py").write_text("# x", encoding="utf-8")
    winners, _ = retrieve(s, "orderkit", ws, "Add a field", k=3)
    assert any(w["memory_type"] == "ValidationParity" for w in winners)  # always-on
    vp = next(w for w in winners if w["memory_type"] == "ValidationParity")
    txt = render_payload(vp)
    assert "before declaring the task done" in txt and "None" not in txt


def test_oracle_attribution_cases_3_4_5():
    """case ③ PD 采纳后本地过 oracle 不过 → PD 负向;
    case ④ VP 采纳 + oracle 过 → VP 正向 adoption_outcome;
    case ⑤ VP 采纳 + oracle 不过 → VP 负向。"""
    s = _vp_store()
    vp_mid = _vp_mid(s)["memory_id"]
    # 种一条 active PD memory(refresh)
    pd_events = [
        {"step_idx": 1, "event_type": "TEST_RUN", "payload": {"command": PYTEST, "passed": False}},
        {"step_idx": 2, "event_type": "FILE_EDIT", "payload": {"path": "src/models.py"}},
        {"step_idx": 3, "event_type": "COMMAND_RUN",
         "payload": {"command": "python3 tools/refresh.py"}},
        {"step_idx": 4, "event_type": "TEST_RUN", "payload": {"command": PYTEST, "passed": True}},
    ]
    for i, fam in enumerate(("add-field", "rename-field")):
        s.ingest_episode(_meta(f"p{i}", fam, started=f"t{3+i}"), pd_events)
        s.relearn(f"p{i}")
    pd_mid = s.con.execute("SELECT memory_id FROM memory_items WHERE canonical_key"
                           " LIKE '%refresh%'").fetchone()["memory_id"]

    # case ③:注入 PD,采纳,本地过,oracle 不过 → PD 负向
    ev3 = [{"step_idx": 0, "event_type": "MEMORY_INJECTED",
            "payload": {"memory_ids": [pd_mid]}}] + pd_events + \
          [{"step_idx": 5, "event_type": "DELAYED_CHECK_RESULT", "payload": {"passed": False}}]
    s.ingest_episode(_meta("c3", "add-field", arm="memory_on", started="t6"), ev3)
    s.relearn("c3")
    neg = s.con.execute(
        "SELECT COUNT(*) c FROM evidence_links WHERE memory_id=? AND polarity<0"
        " AND evidence_source='adoption_outcome'", (pd_mid,)).fetchone()["c"]
    assert neg == 1

    # case ④:注入 VP,采纳(跑 check 过),oracle 过 → VP 正向 adoption_outcome
    ev4 = [{"step_idx": 0, "event_type": "MEMORY_INJECTED",
            "payload": {"memory_ids": [vp_mid]}},
           {"step_idx": 1, "event_type": "TEST_RUN", "payload": {"command": PYTEST, "passed": False}},
           {"step_idx": 2, "event_type": "FILE_EDIT", "payload": {"path": "src/models.py"}},
           {"step_idx": 3, "event_type": "TEST_RUN", "payload": {"command": PYTEST, "passed": True}},
           {"step_idx": 4, "event_type": "TEST_RUN",
            "payload": {"command": CHECK, "passed": True, "source": "check_tool"}},
           {"step_idx": 5, "event_type": "DELAYED_CHECK_RESULT", "payload": {"passed": True}}]
    s.ingest_episode(_meta("c4", "add-field", arm="memory_on", started="t7"), ev4)
    s.relearn("c4")
    rows = s.con.execute(
        "SELECT e.polarity, e.evidence_source FROM evidence_links e"
        " JOIN claims c ON e.claim_id=c.claim_id"
        " WHERE e.memory_id=? AND c.episode_id='c4'", (vp_mid,)).fetchall()
    assert len(rows) == 1 and rows[0]["polarity"] == 1
    assert rows[0]["evidence_source"] == "adoption_outcome"

    # case ⑤:注入 VP,采纳,oracle 仍不过 → VP 负向
    ev5 = [dict(e) for e in ev4]
    ev5[-1] = {"step_idx": 5, "event_type": "DELAYED_CHECK_RESULT", "payload": {"passed": False}}
    s.ingest_episode(_meta("c5", "rename-field", arm="memory_on", started="t8"), ev5)
    s.relearn("c5")
    neg_vp = s.con.execute(
        "SELECT COUNT(*) c FROM evidence_links e JOIN claims c2 ON e.claim_id=c2.claim_id"
        " WHERE e.memory_id=? AND e.polarity<0 AND c2.episode_id='c5'",
        (vp_mid,)).fetchone()["c"]
    assert neg_vp == 1
    # 晋升计数纪律:adoption_outcome 正向不进 support
    belief = json.loads(s.con.execute(
        "SELECT per_context_json FROM belief_states WHERE memory_id=?",
        (vp_mid,)).fetchone()["per_context_json"])
    assert belief["adoption_support"] >= 1
