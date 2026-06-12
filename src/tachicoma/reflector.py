"""Ungoverned baseline 臂 U(FR-40b):Reflector ON + Tachicoma OFF。

科学对照设计:U 复用**同一个 extractor** 产 lesson(提取质量恒定),差异只剩治理——
无 gate、无 dispute/deprecate、无 suppression、无 rival top-1。lesson append-only,
一旦写入永远全量注入(naive reflection memory 的本质)。

预期声明(plan Stage 4,跑批前写死):rotated 世界 U 无 dispute 机制 →
持续注入过期 procedure;治理臂 dispute 后恢复。静态世界 U ≈ B 是预期、不算输。
"""

from __future__ import annotations

import json
from pathlib import Path

from tachicoma.extractor import extract
from tachicoma.path_classifier import Episode


class Reflector:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lessons: list[dict] = (
            json.loads(self.path.read_text()) if self.path.exists() else [])

    def learn(self, ep: Episode) -> int:
        """正向 claim → lesson,append-only(无去重治理,仅防完全相同行重复膨胀)。"""
        added = 0
        seen = {(l["after_edit"], l["must_run"]) for l in self._lessons}
        for c in extract(ep):
            if c.polarity <= 0:
                continue
            key = (c.trigger.get("after_edit", ""), c.action.get("must_run", ""))
            if key in seen:
                continue
            self._lessons.append({"after_edit": key[0], "must_run": key[1]})
            seen.add(key)
            added += 1
        if added:
            self.path.write_text(json.dumps(self._lessons, indent=2))
        return added

    def injection_block(self) -> str:
        if not self._lessons:
            return ""
        head = "Lessons learned from previous tasks in this repository:"
        lines = [f"- After editing {l['after_edit']}, run `{l['must_run']}`."
                 for l in self._lessons]
        return head + "\n" + "\n".join(lines)
