"""CodeKittyAdapter — FR-2 AgentAdapter contract over code-kitty (P0a 实跑路径的代码化).

注入 = prompt 前缀(FR-34 渲染块由 retrieval 提供);MEMORY_INJECTED 事件由 harness
(runner)记录,不属于 adapter。session JSONL → raw_events 的映射在此单点实现,
语义与 path_classifier.from_code_kitty_jsonl 保持一致(cat 工具文件计为 FILE_READ)。
"""

from __future__ import annotations

import os
import re
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

CODE_KITTY_ROOT = Path(os.environ.get("CODE_KITTY_ROOT", Path.home() / "Projects" / "code-kitty"))

_TEST_CMD = re.compile(r"\bpytest\b")
_CHECK_CMD = re.compile(r"\btools/check_\w+\.py\b")   # CI-equivalent 验证工具(FR-9b)
_READ_CMD = re.compile(r"\b(cat|head|tail|less|sed -n)\b")


@dataclass
class EpisodeResult:
    events: list[dict]          # raw_events 行:{step_idx, event_type, payload}
    cost_steps: int             # LLM turns
    cost_tokens: int            # input+output
    agent_version: str
    model_version: str
    session_path: str


def session_to_raw_events(session_path: str | Path, tool_path: str = "tools/refresh.py",
                          start_step: int = 1) -> EpisodeResult:
    events: list[dict] = []
    step = start_step - 1
    llm_calls = 0
    tokens = 0
    agent_version = ""
    model_version = ""
    pending: dict | None = None

    for line in Path(session_path).read_text(encoding="utf-8").splitlines():
        ev = json.loads(line)
        etype = ev.get("event")
        if etype == "task_start":
            agent_version = ev.get("agent_version", "")
            model_version = ev.get("model_version", "")
        elif etype == "after_llm_call":
            llm_calls += 1
            usage = ev.get("usage") or {}
            tokens += usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        elif etype == "before_tool_call":
            pending = ev
        elif etype == "after_tool_call" and pending is not None:
            step += 1
            tool = pending.get("tool_name", "")
            args = pending.get("tool_args", {}) or {}
            out = str((ev.get("result") or {}).get("output") or "")
            if tool == "read_file":
                events.append({"step_idx": step, "event_type": "FILE_READ",
                               "payload": {"path": args.get("path")}})
            elif tool in ("edit_file", "write_file"):
                events.append({"step_idx": step, "event_type": "FILE_EDIT",
                               "payload": {"path": args.get("path")}})
            elif tool == "run_bash":
                cmd = str(args.get("command", ""))
                if _TEST_CMD.search(cmd):
                    failed = re.search(r"\b\d+ failed", out)
                    passed = re.search(r"\b\d+ passed", out)
                    events.append({"step_idx": step, "event_type": "TEST_RUN",
                                   "payload": {"command": cmd,
                                               "passed": bool(passed and not failed)}})
                elif _CHECK_CMD.search(cmd):
                    # FR-9b(R2'-2):check 工具 → 带 passed 字段的 TEST_RUN,
                    # 否则 VP 提取的 fail→pass 锚无输入。成败按输出标记/退出码线索
                    ok = bool(re.search(r"\bOK\b|\bPASS(ED)?\b|contract ok", out, re.I)) \
                        and not re.search(r"\bFAIL(ED)?\b|Error|Traceback|mismatch", out)
                    events.append({"step_idx": step, "event_type": "TEST_RUN",
                                   "payload": {"command": cmd, "passed": ok,
                                               "source": "check_tool"}})
                elif tool_path in cmd and _READ_CMD.search(cmd):
                    events.append({"step_idx": step, "event_type": "FILE_READ",
                                   "payload": {"path": tool_path, "via": cmd}})
                else:
                    events.append({"step_idx": step, "event_type": "COMMAND_RUN",
                                   "payload": {"command": cmd}})
            pending = None

    return EpisodeResult(events=events, cost_steps=llm_calls, cost_tokens=tokens,
                         agent_version=agent_version, model_version=model_version,
                         session_path=str(session_path))


class CodeKittyAdapter:
    """AgentAdapter 契约:in = task(+注入前缀) + workspace;out = EpisodeResult。"""

    def __init__(self, code_kitty_root: Path = CODE_KITTY_ROOT, max_steps: int = 40):
        self.root = Path(code_kitty_root)
        self.max_steps = max_steps
        self.sessions_dir = self.root / ".agent" / "sessions"

    def run(self, task: str, workspace: Path, model: str,
            injection_block: str = "", timeout: int = 900,
            start_step: int = 1) -> EpisodeResult:
        full_task = f"{injection_block}\n\n{task}".strip() if injection_block else task
        before = set(self.sessions_dir.glob("*.jsonl")) if self.sessions_dir.exists() else set()
        env = dict(os.environ)
        env["AGENT_SELF_EVOLUTION"] = "false"   # FR-1 总开关:Tachicoma 驱动时必须 OFF
        env["AGENT_MODEL"] = model
        subprocess.run(
            [str(self.root / ".venv" / "bin" / "python"), "main.py", full_task,
             "--config", "config/local.yaml", "--workspace", str(workspace),
             "--max-steps", str(self.max_steps), "--permission", "auto"],
            cwd=str(self.root), env=env, capture_output=True, text=True, timeout=timeout,
        )
        after = set(self.sessions_dir.glob("*.jsonl"))
        new = sorted(after - before, key=lambda p: p.name)
        if not new:
            raise RuntimeError("code-kitty produced no session JSONL — run failed to start")
        # step 0 = MEMORY_INJECTED(若有)、step 1 = harness pristine check;工具事件随后
        return session_to_raw_events(new[-1], start_step=start_step)
