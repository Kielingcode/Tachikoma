"""EntityResolver — canonical identity for facts (FR-14) + minimal normalization (裁决 3c).

Normalization is deliberately a closed rule set covering only observed cracks:
1. interpreter folding      (python|python3 -> python3)
2. path relativization      (absolute workspace paths -> workspace-relative)
3. whitespace collapsing
4. redirection stripping    (`... 2>&1`, `>out` — norm-v2; observed in P0b Stage 6:
                             one rename trajectory keyed `refresh.py 2>&1` and broke
                             family counting / promotion)
Rules are versioned via NORMALIZER_VERSION; changing them requires a relearn replay
(identity changes are re-keyed through the idempotent cascade).
"""

from __future__ import annotations

import re

NORMALIZER_VERSION = "norm-v2"

_WS = re.compile(r"\s+")
_PY = re.compile(r"\bpython(?:3(?:\.\d+)?)?\b")
_REDIR_FUSED = re.compile(r"^\d*>>?")          # 2>&1, >out.txt, 2>/dev/null, >>log
_REDIR_BARE = re.compile(r"^(\d*>>?|<)$")      # bare operator followed by a target token


def _strip_redirections(tokens: list[str]) -> list[str]:
    out: list[str] = []
    skip_next = False
    for t in tokens:
        if skip_next:
            skip_next = False
            continue
        if _REDIR_BARE.match(t):
            skip_next = "&" not in t
            continue
        if _REDIR_FUSED.match(t):
            continue
        out.append(t)
    return out


def normalize_command(cmd: str, workspace_markers: tuple[str, ...] = ("src/", "tests/", "tools/", "build/")) -> str:
    cmd = _WS.sub(" ", cmd.strip())
    cmd = _PY.sub("python3", cmd)
    # path relativization: strip any absolute prefix before a workspace marker
    parts = []
    for token in _strip_redirections(cmd.split(" ")):
        if "/" in token:
            for marker in workspace_markers:
                idx = token.find(marker)
                if idx > 0:
                    token = token[idx:]
                    break
        parts.append(token)
    return " ".join(parts)


def normalize_path(path: str) -> str:
    path = path.replace("\\", "/").strip()
    if path.startswith("./"):
        path = path[2:]
    for marker in ("src/", "tests/", "tools/", "build/"):
        idx = path.find(marker)
        if idx > 0:
            return path[idx:]
    return path


def canonical_key(memory_type: str, trigger: dict, action: dict) -> str:
    """Deterministic identity from schema slots — immune to LLM naming noise."""
    if memory_type == "ProceduralDependency":
        t = normalize_path(trigger["after_edit"])
        a = normalize_command(action["must_run"])
        return f"ProceduralDependency|{t}|{a}"
    if memory_type == "ValidationParity":
        # FR-9b(v2.6)定稿:trigger 槽固定 declare_done,单 action 槽
        a = normalize_command(action["must_run"])
        return f"ValidationParity|declare_done|{a}"
    raise ValueError(f"unknown memory_type: {memory_type!r}")


def rival_key(memory_type: str, scope_repo: str, trigger: dict) -> str:
    """Same type+scope+trigger slot, different action slot => rivals (FR-26)."""
    if memory_type == "ProceduralDependency":
        return f"{memory_type}|{scope_repo}|{normalize_path(trigger['after_edit'])}"
    if memory_type == "ValidationParity":
        # trigger 槽分组——与 PD 不同组,跨类型天然并存注入(S15)
        return f"{memory_type}|{scope_repo}|declare_done"
    raise ValueError(f"unknown memory_type: {memory_type!r}")
