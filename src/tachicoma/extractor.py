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
from tachicoma.resolver import NORMALIZER_VERSION, normalize_command, normalize_path

EXTRACTOR_VERSION = f"pd-v2+{NORMALIZER_VERSION}"

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
    candidate = None
    for a in actions:
        if start < a.step <= end and a.command:
            for segment in _segments(a.command):
                if segment.strip() and not _is_noise(segment):
                    candidate = Action(a.step, "run", command=segment.strip())
    return candidate


def _segments(cmd: str) -> list[str]:
    """Split compound shell commands (cd X && python3 tools/refresh.py && pytest)."""
    out = []
    for part in cmd.split("&&"):
        part = part.strip()
        if part.startswith("cd "):
            continue
        out.append(part)
    return out


def _strip_command(cmd: str) -> str:
    return normalize_command(cmd)
