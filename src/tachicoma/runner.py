"""Episode 编排(architecture §3.1 runtime flow 的代码化)。

备工作区 → 检索注入(memory_on)→ adapter 运行 → harness success_check →
ingest(append-only)→ 入队 relearn(learn=True 时)。
MEMORY_INJECTED 由 harness 记为 step 0 事件(FR-8——没有它 P9/FR-25 不可计算)。
"""

from __future__ import annotations

import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from tachicoma.adapter import CodeKittyAdapter
from tachicoma.generator import materialize
from tachicoma.path_classifier import Action, Episode, classify
from tachicoma.retrieval import injection_block, retrieve
from tachicoma.worlds import world_for


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def success_check(workspace: Path, timeout: int = 120) -> bool:
    """Harness 侧 true check:与 agent 自报无关(FR-9)。"""
    r = subprocess.run(["python3", "-m", "pytest", "tests/", "-q"],
                       cwd=str(workspace), capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0


def events_to_actions(events: list[dict]) -> list[Action]:
    """raw_events 行 → 归一化 Action(与 store.episode_view 同语义)。"""
    actions: list[Action] = []
    for ev in events:
        p, et = ev.get("payload", {}), ev["event_type"]
        if et == "FILE_READ":
            actions.append(Action(ev["step_idx"], "read", path=p.get("path")))
        elif et == "FILE_EDIT":
            actions.append(Action(ev["step_idx"], "edit", path=p.get("path")))
        elif et == "COMMAND_RUN":
            actions.append(Action(ev["step_idx"], "run", command=p.get("command")))
        elif et == "TEST_RUN":
            actions.append(Action(ev["step_idx"], "test_run", command=p.get("command"),
                                  test_passed=p.get("passed")))
        elif et == "DELAYED_CHECK_RESULT":   # FR-9b:oracle 结果 = 合法 outcome 信号
            actions.append(Action(ev["step_idx"], "oracle_check",
                                  test_passed=p.get("passed")))
    return actions


def first_try_success(ep: Episode) -> bool:
    """首个 post-edit test cycle 即通过(与 PathClassifier 的时间锚点同语义)。"""
    first_edit = next((a.step for a in ep.actions if a.kind == "edit"), None)
    if first_edit is None:
        return False
    t = next((a for a in ep.actions if a.kind == "test_run" and a.step > first_edit), None)
    return bool(t and t.test_passed)


def run_episode(store, variant_id: str, *, arm: str, model: str, memory_on: bool,
                workspace_root: Path, learn: bool = True, k: int = 3,
                adapter: CodeKittyAdapter | None = None) -> dict:
    adapter = adapter or CodeKittyAdapter()
    episode_id = f"{arm}-{variant_id}-{uuid.uuid4().hex[:6]}"
    ws = Path(workspace_root) / episode_id
    bundle = materialize(variant_id, ws)

    injected, suppressed, block = [], [], ""
    if memory_on:
        injected, suppressed = retrieve(store, bundle.repo, ws, bundle.prompt, k=k)
        block = injection_block(injected)

    # Harness pristine check(机检初始状态,Environment verifies):TDD fixture 按构造
    # 必失败;记为 step-1 TEST_RUN 事件,使"失败前主动探索 → first-try 通过"的轨迹
    # 仍然存在 fail→pass 翻转锚点(§6.3 提取规则本身不变)。若 pristine 反而通过,
    # 说明 variant 物化坏了,立刻拒绝。
    if success_check(ws):
        raise RuntimeError(f"variant {variant_id}: pristine fixture unexpectedly passes")

    started = _now()
    res = adapter.run(bundle.prompt, ws, model, injection_block=block, start_step=2)
    eventual = success_check(ws)

    events = [{"step_idx": 1, "event_type": "TEST_RUN",
               "payload": {"command": bundle.test_command, "passed": False,
                           "source": "harness_pristine_check"}}] + list(res.events)
    if injected:
        events.insert(0, {"step_idx": 0, "event_type": "MEMORY_INJECTED",
                          "payload": {"memory_ids": [i["memory_id"] for i in injected],
                                      "rival_suppressed": suppressed}})  # NFR-8 审计

    wp = world_for(bundle.generator_template)   # P1 Stage 3.2:路径参数随世界走
    ep = Episode(actions=events_to_actions(events), eventual_success=eventual,
                 cost_steps=res.cost_steps, cost_tokens=res.cost_tokens,
                 memory_injected=bool(injected),
                 trigger_path=wp.trigger_path, tool_path=wp.tool_path,
                 derived_paths=wp.derived_paths, golden_paths=wp.golden_paths)
    pc = classify(ep)

    store.ingest_episode({
        "episode_id": episode_id, "task_id": bundle.task_id,
        "family_id": bundle.family_id, "generator_template": bundle.generator_template,
        "arm": arm, "repo": bundle.repo,
        "model_version": res.model_version or model,
        "agent_version": res.agent_version,
        "started_at": started, "ended_at": _now(),
        "first_try_success": int(first_try_success(ep)),
        "eventual_success": int(eventual),
        "cost_steps": res.cost_steps, "cost_tokens": res.cost_tokens,
        "wrong_turn_count": None,
    }, events)
    if learn:
        store.relearn(episode_id)

    return {"episode_id": episode_id, "variant_id": variant_id,
            "family_id": bundle.family_id, "arm": arm, "model": model,
            "memory_on": memory_on, "injected": [i["memory_id"] for i in injected],
            "first_try": first_try_success(ep), "eventual": eventual,
            "cost_steps": res.cost_steps, "cost_tokens": res.cost_tokens,
            "path_class": pc.as_dict(), "session": res.session_path,
            "fact_oracle": bundle.fact_oracle}
