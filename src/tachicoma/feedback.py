"""FR-8b 跨 episode 延迟反馈通道(P2.1)= 环境事实,非注入面。

上一批同族任务 oracle fail → 下一批任务 prompt 前的密封反馈文案,给 agent
任务侧动机去探索验证面(VP 的 organic 上车点)。

边界(spec):
- 走 FR-8(环境事实),不走 FR-33——反馈不是 governed memory;
- 泄漏:命名失败【维度】(wire 兼容),绝不命名验证工具或修法;
- P16 自包含:反馈作为 episode N 的 raw_event 落账,且生成 = 上批
  DELAYED_CHECK_RESULT(raw_event)→ 密封文案的纯函数变换,不缓存可变字段;
- 文案是 spec(改文案破坏既有 raw_event 重放一致性),默认 Level 2。
"""

from __future__ import annotations

import hashlib

# 两档文案(spec,逐字锁定)。默认 Level 2;Level 1 仅 ablation/P2.2。
FEEDBACK_TEXT = {
    1: ("注意:前一批同类任务通过了本地测试,但在 wire 兼容性检查中发现不兼容"
        "——本次请确认完整性。"),
    2: ("注意:前一批同类任务通过了本地测试,但在外部 wire 兼容性检查中失败。"
        "本次完成前,请确认是否需要执行仓库中与当前任务相关的额外验证步骤。"),
}
DEFAULT_LEVEL = 2


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def last_family_oracle_fail(store, repo: str, family_id: str, before: str):
    """纯函数:同 repo+family、started_at < before 的最近一条 oracle-fail episode。
    返回 (episode_id, oracle_check_step) 或 None。仅读 raw_events(P16 自包含)。"""
    rows = store.con.execute(
        "SELECT e.episode_id, e.started_at FROM episodes e"
        " WHERE e.repo=? AND e.family_id=? AND e.started_at<?"
        " ORDER BY e.started_at DESC", (repo, family_id, before)).fetchall()
    for ep in rows:
        oc = store.con.execute(
            "SELECT step_idx, payload_json FROM raw_events WHERE episode_id=?"
            " AND event_type='DELAYED_CHECK_RESULT' ORDER BY step_idx", (ep["episode_id"],)
        ).fetchall()
        import json
        for r in oc:
            if json.loads(r["payload_json"]).get("passed") is False:
                return ep["episode_id"], r["step_idx"]
    return None


def build_feedback(store, repo: str, family_id: str, before: str,
                   level: int = DEFAULT_LEVEL) -> dict | None:
    """若上批同族有 oracle-fail → 返回密封反馈 payload(供 runner 注入 prompt +
    作 raw_event 落账);否则 None。payload 含 source metadata 五字段。"""
    hit = last_family_oracle_fail(store, repo, family_id, before)
    if hit is None:
        return None
    src_eid, oracle_step = hit
    text = FEEDBACK_TEXT[level]
    return {
        "text": text,
        "feedback_source_episode_id": src_eid,
        "feedback_source_oracle_check_id": f"{src_eid}#{oracle_step}",
        "feedback_family_scope": family_id,
        "feedback_oracle_type": "wire_compatibility",
        "feedback_text_hash": _text_hash(text),
        "level": level,
    }


# ---- fixture 版本标记(FR fixture-versioning,P2.1 Stage A-5)----

def write_fixture_version(dest, key: str, rev: str) -> str:
    """fixture builder 调用:写 fixture_version.txt(键名 + rev + 内容哈希)。
    内容哈希 = dest 下所有文件按相对路径排序的串联 sha256,使"改了内容没改键名"
    可被检测(B1 命名混淆教训)。返回哈希。"""
    from pathlib import Path
    d = Path(dest)
    h = hashlib.sha256()
    for p in sorted(d.rglob("*")):
        if p.is_file() and p.name != "fixture_version.txt" and "__pycache__" not in str(p):
            h.update(str(p.relative_to(d)).encode())
            h.update(p.read_bytes())
    digest = h.hexdigest()[:16]
    (d / "fixture_version.txt").write_text(
        f"key={key}\nrev={rev}\ncontent_sha256={digest}\n", encoding="utf-8")
    return digest
