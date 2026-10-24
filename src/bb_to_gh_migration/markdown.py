"""Helpers to make migrated Markdown safe to post on GitHub."""

import re

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+)(?:.+?)\1", re.DOTALL)
# Bitbucket mentions are either "@username" or "@{account-id}". The lookbehind
# keeps e-mail addresses and already quoted mentions untouched.
_MENTION = re.compile(r"(?<![\w`@])@(\{[^}\s]+\}|[A-Za-z0-9][\w.-]*[A-Za-z0-9_]|[A-Za-z0-9])")


def neutralize_mentions(text: str) -> str:
    """Wrap @mentions in inline code so GitHub does not notify unrelated users.

    Fenced code blocks and inline code spans are left untouched because GitHub
    never turns mentions inside code into notifications.
    """
    lines = text.split("\n")
    out = []
    fence = None
    for line in lines:
        match = _FENCE.match(line)
        if fence is not None:
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
                fence = None
            out.append(line)
            continue
        if match:
            fence = match.group(1)
            out.append(line)
            continue
        out.append(_neutralize_outside_inline_code(line))
    return "\n".join(out)


def _neutralize_outside_inline_code(line: str) -> str:
    parts = []
    last = 0
    for code in _INLINE_CODE.finditer(line):
        parts.append(_MENTION.sub(r"`@\1`", line[last:code.start()]))
        parts.append(code.group(0))
        last = code.end()
    parts.append(_MENTION.sub(r"`@\1`", line[last:]))
    return "".join(parts)
