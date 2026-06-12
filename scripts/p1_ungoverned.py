"""P1 Stage 4 — Ungoverned 基线臂 U(FR-40b,P1-core rotated 侧)。

U = Reflector ON + Tachicoma OFF:同 extractor 产 lesson,append-only,
永远全量注入,无 dispute。静态学习批 3(v4)+ rotated held-out 3(rot_v2a,
与 Stage 3a 同 variants 配对)。episodes 入 u_arm.sqlite 仅审计(不 relearn)。

预期声明(跑批前写死):rotated 世界 U 持续注入过期 procedure 且持续失败;
治理臂(Stage 3a)dispute 后止损——"memory does not rot" 的对照证据。
"""

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tachicoma.adapter import CodeKittyAdapter
from tachicoma.generator import materialize
from tachicoma.path_classifier import Episode, classify
from tachicoma.reflector import Reflector
from tachicoma.runner import events_to_actions, first_try_success, success_check
from tachicoma.store import MemoryStore
from tachicoma.worlds import world_for

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "spikes" / "p1"
STORE = OUT_DIR / "u_arm.sqlite"
LESSONS = OUT_DIR / "u_lessons.json"
RESULTS = OUT_DIR / "ungoverned_results.json"
WS = Path("/tmp/p1_runs/u")
MODEL = "claude-sonnet-4-6"

STATIC_LEARN = ["A1", "B1", "A3"]                       # 静态学习批(v4 学习参数域)
ROTATED = ["R1@rot_v2a", "R2@rot_v2a", "R3@rot_v2a"]    # 与 Stage 3a 同 variants 配对


def run_u(store, adapter, reflector, ref: str, arm: str) -> dict:
    episode_id = f"{arm}-{ref.replace('@', '_')}-{uuid.uuid4().hex[:6]}"
    ws = WS / episode_id
    bundle = materialize(ref, ws)
    block = reflector.injection_block()
    n_lessons = block.count("\n- ") + (1 if block else 0)

    if success_check(ws):
        raise RuntimeError(f"variant {ref}: pristine fixture unexpectedly passes")

    started = datetime.now(timezone.utc).isoformat()
    res = adapter.run(bundle.prompt, ws, MODEL, injection_block=block, start_step=2)
    eventual = success_check(ws)

    events = [{"step_idx": 1, "event_type": "TEST_RUN",
               "payload": {"command": bundle.test_command, "passed": False,
                           "source": "harness_pristine_check"}}] + list(res.events)
    if block:
        events.insert(0, {"step_idx": 0, "event_type": "MEMORY_INJECTED",
                          "payload": {"memory_ids": [],
                                      "ungoverned_lessons": n_lessons}})

    wp = world_for(bundle.generator_template)
    ep = Episode(actions=events_to_actions(events), eventual_success=eventual,
                 cost_steps=res.cost_steps, cost_tokens=res.cost_tokens,
                 memory_injected=bool(block),
                 trigger_path=wp.trigger_path, tool_path=wp.tool_path,
                 derived_paths=wp.derived_paths, golden_paths=wp.golden_paths)
    pc = classify(ep)

    store.ingest_episode({
        "episode_id": episode_id, "task_id": bundle.task_id,
        "family_id": bundle.family_id, "generator_template": bundle.generator_template,
        "arm": arm, "repo": bundle.repo,
        "model_version": res.model_version or MODEL,
        "agent_version": res.agent_version,
        "started_at": started, "ended_at": datetime.now(timezone.utc).isoformat(),
        "first_try_success": int(first_try_success(ep)),
        "eventual_success": int(eventual),
        "cost_steps": res.cost_steps, "cost_tokens": res.cost_tokens,
        "wrong_turn_count": None,
    }, events)                                   # 审计入库;不 relearn(Tachicoma OFF)

    added = reflector.learn(ep)                  # append-only,无治理
    return {"episode_id": episode_id, "variant_id": ref, "arm": arm,
            "lessons_injected": n_lessons, "lessons_added": added,
            "first_try": first_try_success(ep), "eventual": eventual,
            "cost_steps": res.cost_steps, "path_class": pc.as_dict()}


def main() -> None:
    store = MemoryStore(STORE)
    adapter = CodeKittyAdapter()
    reflector = Reflector(LESSONS)
    payload = {"static_learning": [], "rotated": []}

    for i, ref in enumerate(STATIC_LEARN, 1):
        print(f"[U static {i}/{len(STATIC_LEARN)}] {ref}", flush=True)
        r = run_u(store, adapter, reflector, ref, "arm_u_static")
        payload["static_learning"].append(r)
        print(f"    eventual={r['eventual']} lessons_added={r['lessons_added']}",
              flush=True)
        RESULTS.write_text(json.dumps(payload, indent=2))

    for i, ref in enumerate(ROTATED, 1):
        print(f"[U rotated {i}/{len(ROTATED)}] {ref}", flush=True)
        r = run_u(store, adapter, reflector, ref, "arm_u_rotated")
        payload["rotated"].append(r)
        print(f"    eventual={r['eventual']} steps={r['cost_steps']}"
              f" lessons_injected={r['lessons_injected']}", flush=True)
        RESULTS.write_text(json.dumps(payload, indent=2))

    payload["expectation_check"] = {
        "u_rotated_keeps_failing": all(not r["eventual"] for r in payload["rotated"]),
        "u_lessons_never_pruned": True,   # append-only by construction
    }
    RESULTS.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload["expectation_check"], indent=1))


if __name__ == "__main__":
    main()
