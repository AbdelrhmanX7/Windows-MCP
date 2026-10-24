"""Minimal YAML-subset frontmatter parser and serializer for skill files.

Supported (and all the skill format uses):

* ``key: value`` scalars: bare or quoted strings, ints, ``true``/``false``, empty;
* inline lists ``key: [a, "b, c"]`` and block lists (``key:`` then ``  - item`` lines);
* one level of nested mapping (``match:`` then ``  process: [a.exe]``);
* ``#`` comment lines.

PyYAML is deliberately not required: the format stays small enough that any
indexer (or a human) can read and write it.
"""

from __future__ import annotations

import re

_DELIM = "---"
_INLINE_ITEM_RE = re.compile(r'\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[^,]+)\s*(?:,|$)')


class FrontmatterError(ValueError):
    """Raised when a skill file's frontmatter cannot be parsed."""


def split(text: str) -> tuple[str, str]:
    """Split a document into ``(frontmatter_text, body)``; empty frontmatter if absent."""
    text = text.lstrip("﻿")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != _DELIM:
        return "", text
    for index in range(1, len(lines)):
        if lines[index].strip() == _DELIM:
            return "".join(lines[1:index]), "".join(lines[index + 1 :]).lstrip("\r\n")
    raise FrontmatterError("frontmatter opened with '---' but never closed")


def parse_scalar(raw: str):
    value = raw.strip()
    if not value:
        return ""
    if value[0] in "\"'" and value[-1] == value[0] and len(value) >= 2:
        inner = value[1:-1]
        return re.sub(r"\\(.)", r"\1", inner) if value[0] == '"' else inner
    lowered = value.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    if lowered in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _parse_value(raw: str):
    value = raw.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [parse_scalar(m.group(1)) for m in _INLINE_ITEM_RE.finditer(inner) if m.group(1)]
    return parse_scalar(value)


def _strip_comment(line: str) -> str:
    # Only full-line comments; '#' inside values (e.g. "C#") is kept.
    return "" if line.lstrip().startswith("#") else line


def parse(text: str) -> dict:
    """Parse frontmatter text into a dict."""
    result: dict = {}
    current_key: str | None = None  # key whose value is a pending block (list or map)
    current_map_key: str | None = None  # nested key inside a map whose block list is pending
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw_line).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 0:
            if ":" not in stripped:
                raise FrontmatterError(f"line {lineno}: expected 'key: value'")
            key, _, rest = stripped.partition(":")
            key = key.strip()
            if rest.strip():
                result[key] = _parse_value(rest)
                current_key = None
            else:
                result[key] = None  # becomes list or dict on the next indented line
                current_key = key
            current_map_key = None
            continue
        if current_key is None:
            raise FrontmatterError(f"line {lineno}: unexpected indentation")
        container = result[current_key]
        if stripped.startswith("- "):
            item = parse_scalar(stripped[2:])
            if isinstance(container, dict):
                if current_map_key is None:
                    raise FrontmatterError(f"line {lineno}: list item without a key")
                if not isinstance(container.get(current_map_key), list):
                    container[current_map_key] = []
                container[current_map_key].append(item)
            else:
                if container is None:
                    container = result[current_key] = []
                container.append(item)
            continue
        if ":" not in stripped:
            raise FrontmatterError(f"line {lineno}: expected 'key: value' or '- item'")
        if container is None:
            container = result[current_key] = {}
        if not isinstance(container, dict):
            raise FrontmatterError(f"line {lineno}: cannot mix list items and keys")
        sub_key, _, rest = stripped.partition(":")
        sub_key = sub_key.strip()
        if rest.strip():
            container[sub_key] = _parse_value(rest)
            current_map_key = None
        else:
            container[sub_key] = []
            current_map_key = sub_key
    return {k: ("" if v is None else v) for k, v in result.items()}


def _quote(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = "" if value is None else str(value)
    needs_quotes = (
        not text
        or text != text.strip()
        or any(ch in text for ch in ",:[]{}#\"'")
        or text.lower() in ("true", "false", "yes", "no", "null", "~")
        or re.fullmatch(r"-?\d+", text) is not None
        or text.startswith(("-", "*", "&", "!", "|", ">", "%", "@"))
    )
    if needs_quotes:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _dump_value(value) -> str:
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_quote(v) for v in value) + "]"
    return _quote(value)


def dump(data: dict) -> str:
    """Serialize *data* back to frontmatter text (without the ``---`` fences)."""
    lines = []
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{key}:")
            for sub_key, sub_value in value.items():
                lines.append(f"  {sub_key}: {_dump_value(sub_value)}")
        else:
            lines.append(f"{key}: {_dump_value(value)}")
    return "\n".join(lines) + "\n"


def render(data: dict, body: str) -> str:
    """Render a full document: fenced frontmatter followed by the markdown body."""
    return f"{_DELIM}\n{dump(data)}{_DELIM}\n\n{body.strip()}\n"
