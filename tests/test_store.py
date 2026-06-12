"""Store/governance gating tests — S4 幂等、S5 独立性、级联 demote(plan Stage 5)。"""

from tachicoma.store import MemoryStore


def _episode_events(edit_path="src/models.py", cmd="python3 tools/refresh.py"):
    return [
        {"step_idx": 1, "event_type": "FILE_EDIT", "payload": {"path": edit_path}},
        {"step_idx": 2, "event_type": "TEST_RUN",
         "payload": {"command": "python3 -m pytest tests/ -q", "passed": False}},
        {"step_idx": 3, "event_type": "COMMAND_RUN", "payload": {"command": cmd}},
        {"step_idx": 4, "event_type": "TEST_RUN",
         "payload": {"command": "python3 -m pytest tests/ -q", "passed": True}},
    ]


def _meta(eid, family, repo="orderkit"):
    return {"episode_id": eid, "task_id": f"t_{eid}", "family_id": family,
            "generator_template": "hidden_coupling", "arm": "memory_off", "repo": repo,
            "model_version": "test", "agent_version": "test",
            "started_at": "", "ended_at": "", "first_try_success": 0,
            "eventual_success": 1, "cost_steps": 4, "cost_tokens": 100,
            "wrong_turn_count": 0}


def test_s5_single_family_does_not_promote():
    s = MemoryStore()
    for i in range(5):  # 同一 family 5 条证据
        s.ingest_episode(_meta(f"e{i}", "add-field"), _episode_events())
        s.relearn(f"e{i}")
    items = s.con.execute("SELECT status, canonical_key FROM memory_items").fetchall()
    assert len(items) == 1
    assert items[0]["status"] == "candidate"  # families=1 → 不晋升


def test_promotion_after_second_family():
    s = MemoryStore()
    s.ingest_episode(_meta("e1", "add-field"), _episode_events())
    s.relearn("e1")
    s.ingest_episode(_meta("e2", "rename-field"), _episode_events())
    s.relearn("e2")
    row = s.con.execute("SELECT status FROM memory_items").fetchone()
    assert row["status"] == "active_correlational"
    hist = s.con.execute("SELECT old_status,new_status FROM status_history").fetchall()
    assert (hist[-1]["old_status"], hist[-1]["new_status"]) == ("candidate", "active_correlational")


def test_s4_double_relearn_zero_drift():
    s = MemoryStore()
    s.ingest_episode(_meta("e1", "add-field"), _episode_events())
    s.ingest_episode(_meta("e2", "rename-field"), _episode_events())
    s.relearn("e1"); s.relearn("e2")
    before = s.counts()
    belief_before = dict(s.con.execute("SELECT support_count, distinct_task_family FROM belief_states").fetchone())
    s.relearn("e1"); s.relearn("e1"); s.relearn("e2")  # 重放多次
    after = s.counts()
    belief_after = dict(s.con.execute("SELECT support_count, distinct_task_family FROM belief_states").fetchone())
    # raw 永不变;派生行数与信念零漂移(claim_id 会换,但行数与统计一致)。
    # audit_log 除外:它是 append-only 的作业历史,每次 relearn 合法增长(NFR-4)。
    drift_keys = [k for k in before if k != "audit_log"]
    assert {k: before[k] for k in drift_keys} == {k: after[k] for k in drift_keys}
    assert belief_before == belief_after


def test_cascade_demote_on_evidence_loss():
    """relearn 替换后证据跌破阈值 → 已晋升项必须级联降回(FR-18)。"""
    s = MemoryStore()
    s.ingest_episode(_meta("e1", "add-field"), _episode_events())
    s.ingest_episode(_meta("e2", "rename-field"), _episode_events())
    s.relearn("e1"); s.relearn("e2")
    assert s.con.execute("SELECT status FROM memory_items").fetchone()["status"] == "active_correlational"
    # 模拟重抽取后 e2 不再产出该 claim:直接删 e2 的派生证据再触发重算
    s.con.execute("DELETE FROM evidence_links WHERE claim_id IN (SELECT claim_id FROM claims WHERE episode_id='e2')")
    s.con.execute("DELETE FROM claims WHERE episode_id='e2'")
    s.con.commit()
    s.relearn("e1")  # 任意 relearn 会对受影响项重算+重评
    assert s.con.execute("SELECT status FROM memory_items").fetchone()["status"] == "candidate"


def test_raw_events_never_touched_by_relearn():
    s = MemoryStore()
    s.ingest_episode(_meta("e1", "add-field"), _episode_events())
    raw_before = s.con.execute("SELECT COUNT(*) c FROM raw_events").fetchone()["c"]
    s.relearn("e1"); s.relearn("e1")
    assert s.con.execute("SELECT COUNT(*) c FROM raw_events").fetchone()["c"] == raw_before
