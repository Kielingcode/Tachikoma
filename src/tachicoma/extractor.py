"""ProceduralDependencyExtractor — deterministic, fact-schema-constrained (FR-10/C9).

Positive claim:  edit(X) ... run_cmd(C) ... first fail->pass flip
                 => ProceduralDependency(after_edit=X, must_run=C), polarity +1
Negative claim:  injected memory adopted (per PathClassifier) and the episode
                 still failed => polarity -1 (P9 dispute fuel)

Imperfect extraction is tolerated by design: claims are evidence, not beliefs (P3);
family counting, the promotion gate, and canaries filter wrong extractions, and
oracle precision (FR-43, eval-only) measures the error rate.
"""

from __future__ import annotations

from dataclasses import dataclass

from tachicoma.path_classifier import Action, Episode
from tachicoma.resolver import (NORMALIZER_VERSION, check_segments, classify_segment,
                                normalize_command, normalize_path, split_segments)

EXTRACTOR_VERSION = f"pd-v3-vp-v1+{NORMALIZER_VERSION}"   # FR-14b/FR-9b(P2)

# Commands that are never load-bearing procedures (validation/inspection noise).
# Matched on the segment's FIRST TOKEN (basename) — substring matching is a trap
# ("tools/refresh.py" contains "ls").
_NOISE_CMDS = {"ls", "cat", "head", "tail", "grep", "find", "diff", "git", "echo",
               "pwd", "wc", "sed", "awk", "tree", "which"}


def _is_noise(segment: str) -> bool:
    if "pytest" in segment:
        return True
    tokens = segment.split()
    if not tokens:
        return True
    first = tokens[0].rsplit("/", 1)[-1]
    if first in _NOISE_CMDS:
        return True
    # `python3 -c ...` inline snippets are inspection, not procedures
    if first.startswith("python") and len(tokens) > 1 and tokens[1] == "-c":
        return True
    return False


@dataclass
class ExtractedClaim:
    claim_type: str            # ProceduralDependency
    trigger: dict              # {"after_edit": path}
    action: dict               # {"must_run": command}
    polarity: int              # +1 / -1
    grounding_start_step: int
    grounding_end_step: int
    memory_id: str | None = None   # set on negative claims about injected memories


def extract(ep: Episode) -> list[ExtractedClaim]:
    claims: list[ExtractedClaim] = []
    claims.extend(_extract_validation_parity(ep))

    flip = _first_fail_to_pass(ep.actions)
    if flip is not None:
        flip_step = flip.step
        for edit in [a for a in ep.actions if a.kind == "edit" and a.step < flip_step]:
            if not _is_source_edit(edit):
                continue
            cmd_action = _last_procedure_between(ep.actions, edit.step, flip_step)
            if cmd_action is None:
                continue
            claims.append(ExtractedClaim(
                claim_type="ProceduralDependency",
                trigger={"after_edit": normalize_path(edit.path)},
                action={"must_run": _strip_command(cmd_action.command)},
                polarity=+1,
                grounding_start_step=edit.step,
                grounding_end_step=flip_step,
            ))

    return claims


def _extract_validation_parity(ep: Episode) -> list[ExtractedClaim]:
    """FR-9b(v2.6)第二条确定性规则:ValidationParity。

    锚点 = **check 命令自身**的 fail→pass 翻转(同一归一化命令,先 fail 后 pass),
    且翻转后首个 oracle_check(DELAYED_CHECK_RESULT)通过 → 正向 VP claim。
    本地 test 套件(pytest)按构造排除——它是"说谎方",不能成为 VP 的 action
    (FR-14b 归因规则的提取侧镜像)。VP 锚定 check 命令自身翻转 + oracle 确认,
    与 PD 的 edit→procedure→suite-flip 形状正交,不会互相坍缩。
    """
    by_cmd: dict[str, list[Action]] = {}
    for a in ep.actions:
        if a.kind == "test_run" and a.command:
            # FR-14b:复合命令按段归因——VP 取排除本地套件后的 validation 段
            for seg in check_segments(a.command):
                by_cmd.setdefault(seg, []).append(a)

    out: list[ExtractedClaim] = []
    for cmd, runs in by_cmd.items():
        flip_step, seen_fail = None, None
        for a in runs:
            if a.test_passed is False and seen_fail is None:
                seen_fail = a.step
            elif a.test_passed and seen_fail is not None:
                flip_step = a.step
                break
        if flip_step is None:
            continue
        oracle_after = next((a for a in ep.actions
                             if a.kind == "oracle_check" and a.step > flip_step), None)
        if oracle_after is None or not oracle_after.test_passed:
            continue
        out.append(ExtractedClaim(
            claim_type="ValidationParity",
            trigger={"before": "declare_done"},
            action={"must_run": cmd},
            polarity=+1,
            grounding_start_step=seen_fail,
            grounding_end_step=oracle_after.step,
        ))
    return out


def _is_source_edit(a: Action) -> bool:
    p = normalize_path(a.path or "")
    return p.startswith("src/")


def _first_fail_to_pass(actions: list[Action]) -> Action | None:
    seen_fail = False
    for a in actions:
        if a.kind == "test_run":
            if a.test_passed is False:
                seen_fail = True
            elif a.test_passed and seen_fail:
                return a
    return None


def _last_procedure_between(actions: list[Action], start: int, end: int) -> Action | None:
    """PD 归因(FR-14b):最后一个 **mutation** 段——validation 段(pytest/check)
    不抢归因(堵 `migrate.py && pytest` 把 action 抢到 pytest 的坑)。"""
    candidate = None
    for a in actions:
        if start < a.step <= end and a.command:
            for segment in split_segments(a.command):
                if classify_segment(segment) == "mutation" and not _is_noise(segment):
                    candidate = Action(a.step, "run", command=segment.strip())
    return candidate


def _strip_command(cmd: str) -> str:
    return normalize_command(cmd)
