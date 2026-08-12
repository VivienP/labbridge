"""Tests for the no_prose_churn PreToolUse guard.

Self-contained: this hook lives under the gitignored `.claude/` tree, so the test imports the
module by path rather than relying on the `tests/` collection root. Run with:

    python -m pytest .claude/hooks/test_no_prose_churn.py -v
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

_HOOK_PATH = pathlib.Path(__file__).with_name("no_prose_churn.py")
_spec = importlib.util.spec_from_file_location("no_prose_churn", _HOOK_PATH)
assert _spec and _spec.loader
no_prose_churn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(no_prose_churn)
is_pure_prose_churn = no_prose_churn.is_pure_prose_churn

LINE_LENGTH = 100


# --- Formatting-only no-ops: MUST be detected as churn (the hook denies these) ---

CHURN = {
    "docstring re-wrap, identical words": (
        '    """Mean s_macro per (partition, fold) for one method/budget (averaged over\n'
        '    seed if random)."""\n',
        '    """Mean s_macro per (partition, fold) for one method/budget\n'
        '    (averaged over seed if random)."""\n',
    ),
    "docstring internal newline collapsed to space": (
        '    """first line\n    second line"""\n',
        '    """first line second line"""\n',
    ),
    "adjacent literals re-split, identical runtime value": (
        "    msg = (\n"
        '        "coverage (20/20) and the 7-point partition-"\n'
        '        "robustness gate all pass; supported iff done"\n'
        "    )\n",
        "    msg = (\n"
        '        "coverage (20/20) and the 7-point "\n'
        '        "partition-robustness gate all pass; supported iff done"\n'
        "    )\n",
    ),
    "text moved across string/f-string boundary, identical value": (
        "    msg = (\n"
        '        "ESM-uncertainty supported iff "\n'
        '        f"the same gate passes at B={b}; either is null"\n'
        "    )\n",
        "    msg = (\n"
        '        "ESM-uncertainty "\n'
        '        f"supported iff the same gate passes at B={b}; either is null"\n'
        "    )\n",
    ),
    "trailing space redistributed across pieces": (
        '    s = (\n        "gate all pass for S_macro-AUC;"\n        " ESM-uncertainty"\n    )\n',
        '    s = (\n        "gate all pass for S_macro-AUC; "\n        "ESM-uncertainty"\n    )\n',
    ),
    "double space inside a comment": (
        "    mu = esm_prior_mu(scored, revealed)  # posterior mean\n",
        "    mu = esm_prior_mu(scored, revealed)  # posterior  mean\n",
    ),
}


# --- Real edits or legitimate wraps: MUST NOT be flagged (the hook allows these) ---

ALLOWED = {
    "wording change: a word removed": (
        '    """Robustness gate for one contrast (protocol\n    amendment 1)."""\n',
        '    """Robustness gate for one contrast\n    (amendment 1)."""\n',
    ),
    "punctuation swap: comma added": (
        '    """averaged over seed if random."""\n',
        '    """averaged over seed, if random."""\n',
    ),
    "grammar swap: semicolon+verb form": (
        '    """revealed labels; demonstrates the tautology."""\n',
        '    """revealed labels, demonstrating the tautology."""\n',
    ),
    "code change: identifier renamed": (
        "    esm = {sv.variant: sv.delta_g for sv in scored}\n",
        "    esm_map = {sv.variant: sv.delta_g for sv in scored}\n",
    ),
    "code-only re-layout, no prose touched": (
        "    result = call(one, two, three, four)\n",
        "    result = call(\n        one, two, three, four\n    )\n",
    ),
    "typo fix inside a comment": (
        "    x = 1  # teh value\n",
        "    x = 1  # the value\n",
    ),
    "identical old and new": ('    """same"""\n', '    """same"""\n'),
    "unparseable fragment (fail-open)": (
        "    values = ([1, 2, 3   # unterminated\n",
        "    values = ([1, 2, 3  # unterminated\n",
    ),
}


@pytest.mark.parametrize("name", list(CHURN))
def test_churn_is_denied(name: str) -> None:
    old, new = CHURN[name]
    assert is_pure_prose_churn(old, new, line_length=LINE_LENGTH) is True


@pytest.mark.parametrize("name", list(ALLOWED))
def test_real_edits_are_allowed(name: str) -> None:
    old, new = ALLOWED[name]
    assert is_pure_prose_churn(old, new, line_length=LINE_LENGTH) is False


def test_wrapping_an_over_limit_line_is_allowed() -> None:
    """Wrapping a line that exceeds the limit may be a legitimate E501 fix — never blocked."""
    long_line = '    """' + "word " * 30 + 'tail."""\n'  # first physical line over the limit
    assert any(len(line) > LINE_LENGTH for line in long_line.splitlines())
    wrapped = '    """' + "word " * 15 + "\n    " + "word " * 15 + 'tail."""\n'
    assert is_pure_prose_churn(long_line, wrapped, line_length=LINE_LENGTH) is False


# --- End-to-end: the hook process reads PreToolUse JSON on stdin and emits a decision on stdout ---


def _run_hook(payload: dict[str, object]) -> dict[str, object]:
    proc = subprocess.run(
        [sys.executable, str(_HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out: dict[str, object] = json.loads(proc.stdout) if proc.stdout.strip() else {}
    return out


def test_e2e_denies_churn_edit() -> None:
    old, new = CHURN["docstring re-wrap, identical words"]
    out = _run_hook(
        {"tool_name": "Edit", "tool_input": {"old_string": old, "new_string": new}, "cwd": "."}
    )
    decision = out["hookSpecificOutput"]
    assert isinstance(decision, dict)
    assert decision["permissionDecision"] == "deny"


def test_e2e_allows_real_edit() -> None:
    old, new = ALLOWED["code change: identifier renamed"]
    out = _run_hook(
        {"tool_name": "Edit", "tool_input": {"old_string": old, "new_string": new}, "cwd": "."}
    )
    assert out == {}  # no decision -> the edit proceeds


def test_e2e_ignores_non_edit_tools() -> None:
    out = _run_hook(
        {"tool_name": "Write", "tool_input": {"file_path": "x.py", "content": "..."}, "cwd": "."}
    )
    assert out == {}
