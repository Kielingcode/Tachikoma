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

NORMALIZER_VERSION = "norm-v3"   # v3(FR-14b):复合命令拆段 + 段分类(fact-type 归因)

# ---- FR-14b:复合命令拆段与分类(P2)----
# 顶层拆 && / ; / ||;不做完整 shell parser(覆盖 coding-agent 常见形态)。
_SEG_SPLIT = re.compile(r"\s*(?:&&|\|\||;)\s*")
_NOISE_FIRST_TOKENS = {"ls", "cat", "head", "tail", "grep", "find", "diff", "git",
                       "echo", "pwd", "wc", "sed", "awk", "tree", "which", "cd"}
_VALIDATION_PAT = re.compile(r"\bpytest\b|tools/check_\w+\.py")


def split_segments(cmd: str) -> list[str]:
    """顶层拆复合命令;丢空段与 cd 段。"""
    out = []
    for seg in _SEG_SPLIT.split(cmd.strip()):
        seg = seg.strip()
        if not seg or seg.startswith("cd "):
            continue
        out.append(seg)
    return out


def classify_segment(seg: str) -> str:
    """'mutation' | 'validation' | 'noise'(FR-14b 归因前置)。

    分类先于噪声滤:pytest/check 工具是 validation(不是噪声也不是 mutation);
    PD 归因只取 mutation 段,VP 归因只取排除本地套件后的 validation 段。
    """
    tokens = seg.split()
    if not tokens:
        return "noise"
    if _VALIDATION_PAT.search(seg):
        return "validation"
    first = tokens[0].rsplit("/", 1)[-1]
    if first in _NOISE_FIRST_TOKENS:
        return "noise"
    if first.startswith("python") and len(tokens) > 1 and tokens[1] == "-c":
        return "noise"
    return "mutation"


def check_segments(cmd: str) -> list[str]:
    """VP 归因:validation 段中排除本地 test 套件(pytest)——FR-14b 排歧。"""
    return [normalize_command(s) for s in split_segments(cmd)
            if classify_segment(s) == "validation" and "pytest" not in s]

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
