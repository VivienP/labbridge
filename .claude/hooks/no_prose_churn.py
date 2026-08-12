#!/usr/bin/env python3
"""PreToolUse guard for `Edit`: deny a formatting-only rewrite of comment/string prose.

An Edit is denied when it only re-wraps, re-spaces, or redistributes the text of comments,
docstrings, or string literals while leaving the wording, the code, and every runtime string value
unchanged. Such an edit is a provable no-op that costs tokens and reviewer attention for zero
meaning (AI_CONTRACT.md, skill `no-ai-narration`).

Detection is meaning-preserving, not string-diffing. Both sides are reduced to a canonical
signature that is invariant to physical line-wrapping and to whitespace *inside* prose, but
sensitive to any change in code, wording, or a string's concatenated value:

  - adjacent string / f-string pieces are concatenated (Python folds them), so re-splitting a
    multi-line string — even moving words across a piece boundary — collapses to one signature;
  - whitespace inside each comment / string is normalized;
  - non-logical newlines (blank lines, bracket continuations) are dropped;
  - every other token (names, operators, numbers, interpolations, indentation) is kept exactly.

Denies only when both fragments tokenize, their signatures are equal, the raw comment/string text
actually differs (a code-only re-layout is out of scope), AND every original line already fits the
ruff line limit (so wrapping a line that exceeds it — a legitimate linter fix — is never blocked).

Fail-open everywhere: an unparseable fragment or any internal error yields no decision (exit 0), so
a guard bug can never wedge editing. Only the `Edit` tool is inspected; all else is untouched.
"""

from __future__ import annotations

import io
import json
import os
import sys
import textwrap
import tokenize
import tomllib


def _token_type(name: str) -> int | None:
    """The int value of a tokenize token type, or None if this interpreter lacks it."""
    value = getattr(tokenize, name, None)
    return value if isinstance(value, int) else None


# f-string component tokens exist from Python 3.12; tolerate their absence on older interpreters.
_FSTRING_MIDDLE = _token_type("FSTRING_MIDDLE")
_FSTRING = {
    t
    for t in (_token_type("FSTRING_START"), _FSTRING_MIDDLE, _token_type("FSTRING_END"))
    if t is not None
}
_STRINGY = {tokenize.STRING, *_FSTRING}
_DEFAULT_LINE_LENGTH = 100


def _normspace(text: str) -> str:
    """Collapse every run of whitespace to a single space and strip the ends."""
    return " ".join(text.split())


def _string_inner(tok: tokenize.TokenInfo) -> str:
    """The literal text a string token contributes to its concatenated value (no prefix/quotes)."""
    if tok.type in _FSTRING:
        # FSTRING_MIDDLE carries literal text; START/END are only delimiters.
        return tok.string if tok.type == _FSTRING_MIDDLE else ""
    body = tok.string
    i = 0
    while i < len(body) and body[i] not in "\"'":  # skip a string prefix (r, b, f, u, rb, ...)
        i += 1
    body = body[i:]
    for quote in ('"""', "'''", '"', "'"):
        if body.startswith(quote) and body.endswith(quote) and len(body) >= 2 * len(quote):
            return body[len(quote) : -len(quote)]
    return body


def _tokenize(src: str) -> list[tokenize.TokenInfo] | None:
    """Tokenize `src`, retrying dedented (fragments are often indented). None if unparseable."""
    src = src.replace("\r\n", "\n").replace("\r", "\n")
    for candidate in (src, textwrap.dedent(src)):
        try:
            return list(tokenize.generate_tokens(io.StringIO(candidate).readline))
        except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
            continue
    return None


def _canonical(src: str) -> tuple[object, ...] | None:
    """Signature of wording + code + string values, invariant to layout. None if unparseable."""
    toks = _tokenize(src)
    if toks is None:
        return None
    sig: list[object] = []
    run: list[str] = []  # inner text of a maximal run of adjacent string literals

    def flush() -> None:
        if run:
            sig.append(("STR", _normspace("".join(run))))
            run.clear()

    for tok in toks:
        if tok.type in (tokenize.NL, tokenize.ENCODING, tokenize.ENDMARKER):
            continue  # pure layout / bookkeeping — ignore so wrapping is invisible
        if tok.type == tokenize.COMMENT:
            flush()
            sig.append(("COM", _normspace(tok.string)))
        elif tok.type in _STRINGY:
            run.append(_string_inner(tok))
        else:  # real code token or structural NEWLINE/INDENT/DEDENT — kept exactly, ends a run
            flush()
            sig.append(("T", tok.type, tok.string))
    flush()
    return tuple(sig)


def _prose_raw(src: str) -> str | None:
    """Concatenated raw text of every comment/string token — detects whether prose was retyped."""
    toks = _tokenize(src)
    if toks is None:
        return None
    return "".join(t.string for t in toks if t.type == tokenize.COMMENT or t.type in _STRINGY)


def is_pure_prose_churn(old: str, new: str, line_length: int = _DEFAULT_LINE_LENGTH) -> bool:
    """True iff the edit only re-formats comment/string prose with no change of meaning or value."""
    if old == new:
        return False
    canon_old, canon_new = _canonical(old), _canonical(new)
    if canon_old is None or canon_new is None:
        return False  # fail-open: cannot prove it is a no-op
    if canon_old != canon_new:
        return False  # wording, code, or a string value changed — a real edit
    if _prose_raw(old) == _prose_raw(new):
        return False  # no comment/string text changed — a code-only re-layout, out of scope
    # An original line over the limit may be a legitimate wrap (E501 fix) — never block that.
    return not any(len(line) > line_length for line in old.replace("\r\n", "\n").split("\n"))


def _ruff_line_length(cwd: str) -> int:
    """The `[tool.ruff] line-length` from pyproject.toml, or the default if unreadable."""
    root = os.environ.get("CLAUDE_PROJECT_DIR") or cwd or "."
    try:
        with open(os.path.join(root, "pyproject.toml"), "rb") as fh:
            data = tomllib.load(fh)
        return int(data["tool"]["ruff"]["line-length"])
    except Exception:
        return _DEFAULT_LINE_LENGTH


def deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return

    if data.get("tool_name") != "Edit":
        return
    tool_input = data.get("tool_input") or {}
    old = str(tool_input.get("old_string") or "")
    new = str(tool_input.get("new_string") or "")
    if not old or not new:
        return

    try:
        churn = is_pure_prose_churn(old, new, _ruff_line_length(str(data.get("cwd") or ".")))
    except Exception:
        return  # fail-open: never let a guard bug block an edit

    if churn:
        deny(
            "Edit blocked (no_prose_churn / no-ai-narration): this change only re-wraps or "
            "re-spaces comment/string prose — the code, the wording, and every runtime string "
            "value are unchanged, so it is a pure-formatting no-op that wastes tokens. Keep the "
            "existing text. This never blocks a wording or meaning fix, a code change, or "
            "wrapping a line that exceeds the line-length limit."
        )


if __name__ == "__main__":
    main()
