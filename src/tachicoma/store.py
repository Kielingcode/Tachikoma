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
from tachicoma.governance import evaluate_gate, recompute_belief
from tachicoma.path_classifier import Action, Episode, classify
from tachicoma.resolver import canonical_key, rival_key

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
            elif et == "MEMORY_INJECTED":
                injected.extend(p.get("memory_ids", []))
        ep = Episode(
            actions=actions,
            eventual_success=bool(meta["eventual_success"]),
            cost_steps=meta["cost_steps"] or 0,
            cost_tokens=meta["cost_tokens"] or 0,
            memory_injected=bool(injected),
        )
        return ep, injected, meta

    # ------------------------------------------------------------------
    # relearn — the only learning entry point (idempotent, C6/NFR-2)
    # ------------------------------------------------------------------

    def relearn(self, episode_id: str) -> dict:
        job_id = f"job_{uuid.uuid4().hex[:8]}"
        cur = self.con.cursor()
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
            source = "organic_task" if meta["arm"].startswith(("memory", "arm")) or True else meta["arm"]
            claims = extract(ep)
            pc = classify(ep)
            # negative claims (P9): adopted injected memory + episode failed
            if injected and pc.intended_procedure_adopted and not ep.eventual_success:
                for mid in injected:
                    row = cur.execute(
                        "SELECT * FROM memory_items WHERE memory_id=?", (mid,)).fetchone()
                    if row:
                        self._insert_claim_and_link(
                            cur, episode_id, json.loads(row["trigger_json"]),
                            json.loads(row["action_json"]), -1, 0, 0, source)
                        affected.add(mid)

            for c in claims:
                mid = self._insert_claim_and_link(
                    cur, episode_id, c.trigger, c.action, c.polarity,
                    c.grounding_start_step, c.grounding_end_step, source)
                affected.add(mid)

            # 5-6. recompute beliefs + gate evaluation (cascade)
            for mid in affected:
                belief = recompute_belief(cur, mid)
                self._apply_gate(cur, job_id, mid, belief)

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
                               g0, g1, source) -> str:
        ckey = canonical_key("ProceduralDependency", trigger, action)
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
                (mid, "ProceduralDependency", ckey, json.dumps(trigger),
                 json.dumps(action), rival_key("ProceduralDependency", repo, trigger),
                 json.dumps({"repo": repo}), "candidate", _now(), _now()))
        cid = f"clm_{uuid.uuid4().hex[:8]}"
        cur.execute(
            "INSERT INTO claims (claim_id, episode_id, claim_type, canonical_key,"
            " proposition_json, polarity, grounding_start_step, grounding_end_step,"
            " extractor_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cid, episode_id, "ProceduralDependency", ckey,
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
