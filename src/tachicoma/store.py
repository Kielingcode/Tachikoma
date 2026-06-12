"""MemoryStore — event-sourced relational registry (architecture §2, FR-37).

Source of truth: raw_events (append-only) + episodes.
Everything else is derived and rebuilt by relearn() — P16.
Deletion boundary (NFR-4): relearn touches ONLY derived rows (claims,
evidence_links; belief_states are recomputed); raw_events/episodes are never
UPDATEd or DELETEd; every replacement is audited; every status change goes
through status_history.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from tachicoma.extractor import EXTRACTOR_VERSION, extract
from tachicoma.governance import (evaluate_demotion, evaluate_gate, evaluate_inert,
                                  recompute_belief)
from tachicoma.path_classifier import Action, Episode, adoption_record, classify
from tachicoma.resolver import canonical_key, normalize_command, rival_key
from tachicoma.worlds import world_for

# learning-excluded arms(终审修订:store 级强制,runner 纪律只是第一道):
# canary/oracle/noise/diagnostic/frontier 类 episodes 照常入库(审计),
# 但 relearn 排除其 claim 提取与信念重算。
LEARNING_EXCLUDED_ARM_PREFIXES = ("canary", "noise", "oracle", "diagnostic", "frontier")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_events (
  event_id     INTEGER PRIMARY KEY,
  episode_id   TEXT NOT NULL,
  step_idx     INTEGER NOT NULL,
  event_type   TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS episodes (
  episode_id         TEXT PRIMARY KEY,
  task_id            TEXT NOT NULL,
  family_id          TEXT,
  generator_template TEXT,
  arm                TEXT NOT NULL,
  repo               TEXT NOT NULL,
  model_version      TEXT NOT NULL,
  agent_version      TEXT NOT NULL,
  started_at TEXT, ended_at TEXT,
  first_try_success  INTEGER,
  eventual_success   INTEGER,
  cost_steps         INTEGER,
  cost_tokens        INTEGER,
  wrong_turn_count   INTEGER
);
CREATE TABLE IF NOT EXISTS claims (
  claim_id             TEXT PRIMARY KEY,
  episode_id           TEXT NOT NULL,
  claim_type           TEXT NOT NULL,
  canonical_key        TEXT NOT NULL,
  proposition_json     TEXT NOT NULL,
  polarity             INTEGER NOT NULL,
  grounding_start_step INTEGER NOT NULL,
  grounding_end_step   INTEGER NOT NULL,
  extractor_version    TEXT NOT NULL,
  created_at           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_links (
  memory_id       TEXT NOT NULL,
  claim_id        TEXT NOT NULL,
  evidence_source TEXT NOT NULL,
  polarity        INTEGER NOT NULL,
  weight          REAL NOT NULL DEFAULT 1.0,
  UNIQUE(memory_id, claim_id)
);
CREATE TABLE IF NOT EXISTS memory_items (
  memory_id       TEXT PRIMARY KEY,
  memory_type     TEXT NOT NULL,
  canonical_key   TEXT NOT NULL UNIQUE,
  trigger_json    TEXT NOT NULL,
  action_json     TEXT NOT NULL,
  rival_key       TEXT NOT NULL,
  scope_json      TEXT NOT NULL,
  status          TEXT NOT NULL,
  causal_verified INTEGER NOT NULL DEFAULT 0,
  created_at TEXT, updated_at TEXT, deprecated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_rival ON memory_items(rival_key);
CREATE TABLE IF NOT EXISTS belief_states (
  memory_id             TEXT PRIMARY KEY,
  support_count         INTEGER NOT NULL DEFAULT 0,
  contradiction_count   INTEGER NOT NULL DEFAULT 0,
  distinct_task_family  INTEGER NOT NULL DEFAULT 0,
  per_context_json      TEXT NOT NULL DEFAULT '{}',
  posterior_lb          REAL,
  first_seen TEXT, last_seen TEXT,
  computed_from_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS status_history (
  id INTEGER PRIMARY KEY,
  memory_id  TEXT NOT NULL,
  old_status TEXT, new_status TEXT NOT NULL,
  reason     TEXT NOT NULL,
  evidence_snapshot_json TEXT,
  job_id     TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  job_id     TEXT NOT NULL,
  action     TEXT NOT NULL,
  target     TEXT NOT NULL,
  detail_json TEXT,
  created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryStore:
    def __init__(self, db_path: str | Path = ":memory:"):
        self.con = sqlite3.connect(str(db_path))
        self.con.row_factory = sqlite3.Row
        self.con.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # Ingestion (source of truth — append only)
    # ------------------------------------------------------------------

    def ingest_episode(self, meta: dict, events: list[dict]) -> None:
        cols = ("episode_id", "task_id", "family_id", "generator_template", "arm",
                "repo", "model_version", "agent_version", "started_at", "ended_at",
                "first_try_success", "eventual_success", "cost_steps", "cost_tokens",
                "wrong_turn_count")
        self.con.execute(
            f"INSERT INTO episodes ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            tuple(meta.get(c) for c in cols),
        )
        for ev in events:
            self.con.execute(
                "INSERT INTO raw_events (episode_id, step_idx, event_type, payload_json, created_at)"
                " VALUES (?,?,?,?,?)",
                (meta["episode_id"], ev["step_idx"], ev["event_type"],
                 json.dumps(ev.get("payload", {})), _now()),
            )
        self.con.commit()

    # ------------------------------------------------------------------
    # Derived reconstruction
    # ------------------------------------------------------------------

    def episode_view(self, episode_id: str) -> tuple[Episode, list[str], sqlite3.Row]:
        """Rebuild the normalized Episode (+ injected memory ids) from raw_events."""
        meta = self.con.execute(
            "SELECT * FROM episodes WHERE episode_id=?", (episode_id,)
        ).fetchone()
        rows = self.con.execute(
            "SELECT * FROM raw_events WHERE episode_id=? ORDER BY step_idx", (episode_id,)
        ).fetchall()
        actions: list[Action] = []
        injected: list[str] = []
        for r in rows:
            p = json.loads(r["payload_json"])
            et = r["event_type"]
            if et == "FILE_READ":
                actions.append(Action(r["step_idx"], "read", path=p.get("path")))
            elif et == "FILE_EDIT":
                actions.append(Action(r["step_idx"], "edit", path=p.get("path")))
            elif et == "COMMAND_RUN":
                actions.append(Action(r["step_idx"], "run", command=p.get("command")))
            elif et == "TEST_RUN":
                actions.append(Action(r["step_idx"], "test_run",
                                      command=p.get("command"),
                                      test_passed=p.get("passed")))
            elif et == "DELAYED_CHECK_RESULT":   # FR-9b:oracle 结果 = 合法 outcome 信号
                actions.append(Action(r["step_idx"], "oracle_check",
                                      test_passed=p.get("passed")))
            elif et == "MEMORY_INJECTED":
                injected.extend(p.get("memory_ids", []))
        w = world_for(meta["generator_template"])   # Stage 3.2:路径按世界参数化
        ep = Episode(
            actions=actions,
            eventual_success=bool(meta["eventual_success"]),
            cost_steps=meta["cost_steps"] or 0,
            cost_tokens=meta["cost_tokens"] or 0,
            memory_injected=bool(injected),
            trigger_path=w.trigger_path,
            tool_path=w.tool_path,
            derived_paths=w.derived_paths,
            golden_paths=w.golden_paths,
        )
        return ep, injected, meta

    # ------------------------------------------------------------------
    # relearn — the only learning entry point (idempotent, C6/NFR-2)
    # ------------------------------------------------------------------

    def relearn(self, episode_id: str) -> dict:
        job_id = f"job_{uuid.uuid4().hex[:8]}"
        cur = self.con.cursor()

        # learning-excluded(store 级强制):审计入库,但不参与提取与信念
        arm_row = self.con.execute(
            "SELECT arm FROM episodes WHERE episode_id=?", (episode_id,)).fetchone()
        if arm_row and str(arm_row["arm"]).startswith(LEARNING_EXCLUDED_ARM_PREFIXES):
            self._audit(cur, job_id, "learning_excluded_skip", episode_id,
                        {"arm": arm_row["arm"]})
            self.con.commit()
            return {"job_id": job_id, "affected": [], "orphans": [],
                    "learning_excluded": True}

        try:
            cur.execute("BEGIN")
            # 1. per-task replace — derived rows ONLY (raw_events/episodes untouched)
            affected = {
                r["memory_id"] for r in cur.execute(
                    "SELECT DISTINCT memory_id FROM evidence_links WHERE claim_id IN"
                    " (SELECT claim_id FROM claims WHERE episode_id=?)", (episode_id,)
                )
            }
            cur.execute(
                "DELETE FROM evidence_links WHERE claim_id IN"
                " (SELECT claim_id FROM claims WHERE episode_id=?)", (episode_id,))
            cur.execute("DELETE FROM claims WHERE episode_id=?", (episode_id,))
            self._audit(cur, job_id, "claims_replaced", episode_id, {})

            # 2-4. re-extract, resolve, link
            ep, injected, meta = self.episode_view(episode_id)
            claims = extract(ep)
            pc = classify(ep)

            # Evidence-class boundary(P0b 用户裁决):被注入且被采纳的 run 上,与注入
            # memory 同 canonical key 的正向 claim 是 adoption-conditioned evidence ——
            # 证明的是"这条 memory 被采纳后有用",不等价于 memory-off 独立发现。
            # 两类分离防自我强化环:注入→照做成功→若计为独立证据→更强→更易注入。
            #   independent (organic_task):      可用于 birth / promotion
            #   adoption_outcome:                utility / confidence / demotion 抵抗;
            #                                    不单独把 candidate 推成 active,
            #                                    不作为 causal verification
            # G1(P1):per-injected-memory 程序级采纳判定。
            # 负向触发 = adopted ∧ post_adoption_first_test_passed is False
            # ——"采纳后局部未修复";episode 最终恢复不再吞掉 stale harm 证据。
            adopted_keys: set[str] = set()
            g1_handled_keys: set[str] = set()   # G1/oracle 归因已落账的 key,组织循环去重
            oracle_checks = [a for a in ep.actions if a.kind == "oracle_check"]
            for mid in injected:
                row = cur.execute(
                    "SELECT * FROM memory_items WHERE memory_id=?", (mid,)).fetchone()
                if not row:
                    continue
                trigger = json.loads(row["trigger_json"])
                action = json.loads(row["action_json"])
                if row["memory_type"] == "ValidationParity":
                    # FR-9b case ④/⑤:VP 采纳 = check cmd 实际被运行;
                    # 锚定 = 采纳步后首个 oracle_check
                    cmd = normalize_command(action.get("must_run", ""))
                    vp_run = next((a for a in ep.actions
                                   if a.kind in ("run", "test_run") and a.command
                                   and normalize_command(a.command) == cmd), None)
                    if vp_run is None:
                        continue   # case ② 注入未采纳:不归因
                    adopted_keys.add(row["canonical_key"])
                    o = next((a for a in oracle_checks if a.step > vp_run.step), None)
                    if o is not None:
                        self._insert_claim_and_link(
                            cur, episode_id, trigger, action,
                            +1 if o.test_passed else -1,
                            vp_run.step, o.step, "adoption_outcome",
                            claim_type="ValidationParity")
                        g1_handled_keys.add(row["canonical_key"])
                        affected.add(mid)
                    continue
                rec = adoption_record(
                    ep, trigger.get("after_edit", ep.trigger_path),
                    action.get("must_run", ""))
                if rec.adopted:
                    adopted_keys.add(row["canonical_key"])
                    if rec.post_adoption_first_test_passed is False:
                        # G1(P1):采纳后首个本地测试失败 → 程序级负向
                        self._insert_claim_and_link(
                            cur, episode_id, trigger, action, -1,
                            rec.adoption_step or 0, rec.adoption_step or 0,
                            "adoption_outcome")
                        affected.add(mid)
                    elif rec.post_adoption_first_test_passed:
                        # FR-9b case ③(oracle 孪生):本地过、oracle 不过 = false
                        # success after adoption → 程序级负向(P9 负向全计)
                        o = next((a for a in oracle_checks
                                  if a.step > (rec.adoption_step or 0)), None)
                        if o is not None and o.test_passed is False:
                            self._insert_claim_and_link(
                                cur, episode_id, trigger, action, -1,
                                rec.adoption_step or 0, o.step, "adoption_outcome")
                            affected.add(mid)

            for c in claims:
                ckey = canonical_key(c.claim_type, c.trigger, c.action)
                if ckey in g1_handled_keys:
                    continue   # 该 key 本 episode 已由 oracle 归因落账,防双计
                src = "adoption_outcome" if ckey in adopted_keys else "organic_task"
                mid = self._insert_claim_and_link(
                    cur, episode_id, c.trigger, c.action, c.polarity,
                    c.grounding_start_step, c.grounding_end_step, src,
                    claim_type=c.claim_type)
                affected.add(mid)

            # 5-6. recompute beliefs + gate evaluation (cascade)
            for mid in affected:
                belief = recompute_belief(cur, mid)
                self._apply_gate(cur, job_id, mid, belief)

            # 6b. 观察期 sweep(P1):disputed memory 被压制后不再被注入、
            # 不再获得新证据——本 episode 的流逝本身就是它的"无回升"观察,
            # 必须触发其窗口重评,否则 deprecate 永不可达(3b 实测冻结)。
            disputed_rows = cur.execute(
                "SELECT memory_id FROM memory_items WHERE status='disputed'"
                " AND json_extract(scope_json,'$.repo')="
                " (SELECT repo FROM episodes WHERE episode_id=?)",
                (episode_id,)).fetchall()    # fetchall:循环体复用 cursor
            for r in disputed_rows:
                if r["memory_id"] not in affected:
                    belief = recompute_belief(cur, r["memory_id"])
                    self._apply_gate(cur, job_id, r["memory_id"], belief)

            # 6c. 惰性剪枝 sweep(FR-25/S13):active memory 最近 K 次注入连续
            # 未被采纳 → deprecated(直接弧;adoption 即抵抗信号)。
            active_rows = cur.execute(
                "SELECT memory_id FROM memory_items WHERE status LIKE 'active%'"
                " AND json_extract(scope_json,'$.repo')="
                " (SELECT repo FROM episodes WHERE episode_id=?)",
                (episode_id,)).fetchall()
            for r in active_rows:
                self._apply_inert(cur, job_id, r["memory_id"])

            # 7. orphan cleanup: candidates with no evidence left
            orphans = [r["memory_id"] for r in cur.execute(
                "SELECT m.memory_id FROM memory_items m LEFT JOIN evidence_links e"
                " ON m.memory_id=e.memory_id WHERE e.memory_id IS NULL AND m.status='candidate'"
            )]
            for mid in orphans:
                cur.execute("DELETE FROM memory_items WHERE memory_id=?", (mid,))
                cur.execute("DELETE FROM belief_states WHERE memory_id=?", (mid,))
                self._audit(cur, job_id, "orphan_removed", mid, {})

            self.con.commit()
            return {"job_id": job_id, "affected": sorted(affected), "orphans": orphans}
        except Exception:
            self.con.rollback()
            raise

    # ------------------------------------------------------------------

    def _insert_claim_and_link(self, cur, episode_id, trigger, action, polarity,
                               g0, g1, source,
                               claim_type: str = "ProceduralDependency") -> str:
        ckey = canonical_key(claim_type, trigger, action)
        row = cur.execute(
            "SELECT memory_id FROM memory_items WHERE canonical_key=?", (ckey,)).fetchone()
        if row:
            mid = row["memory_id"]
        else:
            mid = f"mem_{uuid.uuid4().hex[:8]}"
            meta = cur.execute(
                "SELECT repo FROM episodes WHERE episode_id=?", (episode_id,)).fetchone()
            repo = meta["repo"] if meta else "unknown"
            cur.execute(
                "INSERT INTO memory_items (memory_id, memory_type, canonical_key,"
                " trigger_json, action_json, rival_key, scope_json, status,"
                " causal_verified, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,0,?,?)",
                (mid, claim_type, ckey, json.dumps(trigger),
                 json.dumps(action), rival_key(claim_type, repo, trigger),
                 json.dumps({"repo": repo}), "candidate", _now(), _now()))
        cid = f"clm_{uuid.uuid4().hex[:8]}"
        cur.execute(
            "INSERT INTO claims (claim_id, episode_id, claim_type, canonical_key,"
            " proposition_json, polarity, grounding_start_step, grounding_end_step,"
            " extractor_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cid, episode_id, claim_type, ckey,
             json.dumps({"trigger": trigger, "action": action}), polarity,
             g0, g1, EXTRACTOR_VERSION, _now()))
        cur.execute(
            "INSERT OR IGNORE INTO evidence_links (memory_id, claim_id, evidence_source,"
            " polarity, weight) VALUES (?,?,?,?,1.0)", (mid, cid, source, polarity))
        return mid

    def _apply_gate(self, cur, job_id, memory_id, belief) -> None:
        row = cur.execute(
            "SELECT status FROM memory_items WHERE memory_id=?", (memory_id,)).fetchone()
        if not row:
            return
        old = row["status"]
        new = evaluate_gate(old, belief)
        if new == old:
            # 降级评估(P1):dispute / deprecate —— 证据集 + 观察流的纯函数。
            # tie-break:同 episode 内负向在前(保守:正向在窗口存活最久);
            # 重放会换 claim rowid,故排序键只用不可变锚。
            evidence = cur.execute(
                "SELECT e.polarity, ep.started_at FROM evidence_links e"
                " JOIN claims c ON e.claim_id=c.claim_id"
                " JOIN episodes ep ON c.episode_id=ep.episode_id"
                " WHERE e.memory_id=? ORDER BY ep.started_at, e.polarity",
                (memory_id,)).fetchall()
            # 观察期推进:最后一条证据之后、同 repo 的 learning-eligible episodes
            # 数量 = "无回升"空槽(suppressed memory 无新证据,窗口仍须可推进)
            episodes_since = 0
            if evidence:
                repo_row = cur.execute(
                    "SELECT json_extract(scope_json,'$.repo') r FROM memory_items"
                    " WHERE memory_id=?", (memory_id,)).fetchone()
                excl = " AND ".join(
                    f"arm NOT LIKE '{p}%'" for p in LEARNING_EXCLUDED_ARM_PREFIXES)
                episodes_since = cur.execute(
                    f"SELECT COUNT(*) c FROM episodes WHERE repo=? AND started_at>?"
                    f" AND {excl}",
                    (repo_row["r"], evidence[-1]["started_at"])).fetchone()["c"]
            new = evaluate_demotion(old, [dict(r) for r in evidence], episodes_since)
        if new != old:
            cur.execute(
                "UPDATE memory_items SET status=?, updated_at=? WHERE memory_id=?",
                (new, _now(), memory_id))
            cur.execute(
                "INSERT INTO status_history (memory_id, old_status, new_status, reason,"
                " evidence_snapshot_json, job_id, created_at) VALUES (?,?,?,?,?,?,?)",
                (memory_id, old, new,
                 f"gate: S={belief['support']} F={belief['contra']}"
                 f" families={belief['families']}",
                 json.dumps(belief), job_id, _now()))

    def _apply_inert(self, cur, job_id, memory_id) -> None:
        """FR-25 惰性剪枝:重算该 memory 的注入-采纳序列(raw_events 纯函数),
        最近 K 次注入连续未采纳 → deprecated。"""
        row = cur.execute(
            "SELECT status, trigger_json, action_json FROM memory_items"
            " WHERE memory_id=?", (memory_id,)).fetchone()
        if not row or not row["status"].startswith("active"):
            return
        trigger = json.loads(row["trigger_json"]).get("after_edit", "")
        action = json.loads(row["action_json"]).get("must_run", "")
        adopted_seq: list[bool] = []
        # fetchall 物化:迭代中还会用同一 cursor 查询(cursor 复用会重置结果集)
        inj_rows = cur.execute(
            "SELECT r.payload_json, r.episode_id, ep.arm FROM raw_events r"
            " JOIN episodes ep ON r.episode_id=ep.episode_id"
            " WHERE r.event_type='MEMORY_INJECTED' ORDER BY ep.started_at").fetchall()
        for ev in inj_rows:
            if memory_id not in json.loads(ev["payload_json"]).get("memory_ids", []):
                continue
            if str(ev["arm"]).startswith(LEARNING_EXCLUDED_ARM_PREFIXES):
                continue
            ep, _, _ = self.episode_view(ev["episode_id"])
            adopted_seq.append(adoption_record(ep, trigger, action).adopted)
        new = evaluate_inert(row["status"], adopted_seq)
        if new != row["status"]:
            cur.execute(
                "UPDATE memory_items SET status=?, updated_at=?, deprecated_at=?"
                " WHERE memory_id=?", (new, _now(), _now(), memory_id))
            cur.execute(
                "INSERT INTO status_history (memory_id, old_status, new_status, reason,"
                " evidence_snapshot_json, job_id, created_at) VALUES (?,?,?,?,?,?,?)",
                (memory_id, row["status"], new,
                 f"inert: last {len(adopted_seq[-3:])} injections un-adopted (FR-25)",
                 json.dumps({"adopted_seq": adopted_seq}), job_id, _now()))

    def _audit(self, cur, job_id, action, target, detail) -> None:
        cur.execute(
            "INSERT INTO audit_log (job_id, action, target, detail_json, created_at)"
            " VALUES (?,?,?,?,?)", (job_id, action, target, json.dumps(detail), _now()))

    # ------------------------------------------------------------------
    # Read-side helpers
    # ------------------------------------------------------------------

    def active_items(self, repo: str) -> list[sqlite3.Row]:
        return self.con.execute(
            "SELECT m.*, b.support_count, b.contradiction_count, b.distinct_task_family"
            " FROM memory_items m JOIN belief_states b ON m.memory_id=b.memory_id"
            " WHERE m.status LIKE 'active%'"
            " AND json_extract(m.scope_json, '$.repo')=?", (repo,)).fetchall()

    def counts(self) -> dict:
        out = {}
        for t in ("raw_events", "episodes", "claims", "evidence_links",
                  "memory_items", "belief_states", "status_history", "audit_log"):
            out[t] = self.con.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        return out
