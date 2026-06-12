"""Retriever + PayloadRenderer (FR-33/FR-34, architecture §3.1).

确定性 trigger 过滤的输入只能是 workspace 文件列表与任务 prompt 文本——
本模块的任何接口都不接收 generator 的 family_id / fact_oracle(oracle 防火墙,FR-43;
行为级证明见 tests/test_retrieval.py)。候选集只读 status LIKE 'active%'(S3/P7)。
"""

from __future__ import annotations

import json
from pathlib import Path

_STATUS_RANK = {"active_verified": 0, "active_correlational": 1}


def retrieve(store, repo: str, workspace: Path, prompt: str, k: int = 3,
             ) -> tuple[list[dict], list[dict]]:
    """active* + repo scope + trigger 过滤 + **rival top-1(FR-26)** → (top-k, suppressed)。

    检索状态语义(P1 终审定夺,suppression):disputed/deprecated 的 status 已不带
    'active' 前缀,被 store.active_items 自然排除——disputed 不渲染为行动指令,
    deprecated 永不参与;可检索集内 active_verified > active_correlational。
    每个 rival_key 竞争集只保留 top-1;被压制者随返回值交给 runner 记入
    MEMORY_INJECTED payload(NFR-8 no silent caps)。
    """
    candidates = []
    for row in store.active_items(repo):
        trigger = json.loads(row["trigger_json"])
        path = trigger.get("after_edit", "")
        # VP trigger 规则(v1.1.1 漏项修复):trigger 非路径({"before":"declare_done"}),
        # repo scope 内 always-on——"declare done 前"在每个任务里都会到来
        vp_always_on = row["memory_type"] == "ValidationParity" and not path
        if not path and not vp_always_on:
            continue
        if vp_always_on or _path_in_workspace(path, workspace) or path in prompt:
            candidates.append({
                "memory_id": row["memory_id"],
                "memory_type": row["memory_type"],
                "status": row["status"],
                "causal_verified": bool(row["causal_verified"]),
                "scope": json.loads(row["scope_json"]),
                "trigger": trigger,
                "action": json.loads(row["action_json"]),
                "support_count": row["support_count"],
                "contradiction_count": row["contradiction_count"],
                "distinct_task_family": row["distinct_task_family"],
                "rival_key": row["rival_key"],
            })
    candidates.sort(key=lambda m: (_STATUS_RANK.get(m["status"], 9), -m["support_count"]))
    winners, suppressed, seen_rivals = [], [], set()
    for m in candidates:
        if m["rival_key"] in seen_rivals:
            suppressed.append({"memory_id": m["memory_id"], "rival_key": m["rival_key"],
                               "status": m["status"]})
            continue
        seen_rivals.add(m["rival_key"])
        winners.append(m)
    return winners[:k], suppressed


def _path_in_workspace(rel_path: str, workspace: Path) -> bool:
    return (Path(workspace) / rel_path).exists()


def render_payload(item: dict) -> str:
    """FR-34 memory payload(YAML 形态,逐字段)。"""
    caution = ("observed useful pattern, not yet causally verified"
               if not item["causal_verified"] else "causally verified via paired canary")
    # 渲染分型(v1.1.1):VP 的 trigger 槽不是路径,套 PD 句式会渲染出 "after editing None"
    if item["memory_type"] == "ValidationParity":
        instruction = (f"before declaring the task done, "
                       f"run \"{item['action'].get('must_run')}\" and make it pass")
    else:
        instruction = (f"after editing {item['trigger'].get('after_edit')}, "
                       f"run \"{item['action'].get('must_run')}\" before final validation")
    return "\n".join([
        "memory_item:",
        f"  memory_id: {item['memory_id']}",
        f"  type: {item['memory_type']}",
        f"  status: {item['status']}",
        f"  causal_verified: {str(item['causal_verified']).lower()}",
        f"  scope: {json.dumps(item['scope'])}",
        f"  trigger: {json.dumps(item['trigger'])}",
        f"  instruction: {instruction}",
        f"  evidence: {{support_task_families: {item['distinct_task_family']}, "
        f"contradiction_count: {item['contradiction_count']}}}",
        f"  caution: {caution}",
    ])


def injection_block(items: list[dict]) -> str:
    if not items:
        return ""
    head = ("Relevant memory from previous tasks in this repository "
            "(governed memory; cite memory_id if you act on it):")
    return head + "\n\n" + "\n\n".join(render_payload(i) for i in items)
