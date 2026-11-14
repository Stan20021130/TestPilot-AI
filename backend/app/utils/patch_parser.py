"""TestPilot AI — Unified diff hunk parser.

Extracts the line numbers that were actually added or modified by a pull
request, in NEW-file numbering, from a unified diff hunk block (the
``@@ -a,b +c,d @@`` format used by GitHub, GitLab, Bitbucket and Azure DevOps).

Downstream agents use this to reason at symbol granularity: a changed file is
no longer treated as "every symbol in it changed", only the symbols that
intersect the touched line ranges.
"""

from __future__ import annotations

import re

_HUNK_HEADER_RE = re.compile(r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
                             r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@")

MAX_LINES_PER_FILE = 5000


def parse_added_lines(patch: str | None) -> list[int]:
    """Return the new-file line numbers added or modified by a unified diff.

    Removed (``-``) lines do not advance the new-file counter, context lines
    do. A line starting with ``+++`` after a hunk header is treated as added
    content, not as a file header, because headers only appear before the
    first hunk (and are skipped while no hunk is open).

    Args:
        patch: The raw unified diff for a single file, or None for binary /
            truncated diffs (GitHub omits the patch for these).

    Returns:
        Sorted, de-duplicated list of line numbers in the new file.
    """
    if not patch:
        return []

    added: list[int] = []
    new_line: int | None = None

    for raw_line in patch.splitlines():
        header = _HUNK_HEADER_RE.match(raw_line)
        if header:
            new_line = int(header.group("new_start"))
            continue

        if new_line is None:
            # Still in the pre-hunk preamble (file headers, index line).
            continue

        if raw_line.startswith("\\"):
            # "\ No newline at end of file" — not a content line.
            continue

        if raw_line.startswith("+"):
            added.append(new_line)
            new_line += 1
        elif raw_line.startswith("-"):
            continue
        else:
            # Context line (or a blank line inside the hunk).
            new_line += 1

        if len(added) >= MAX_LINES_PER_FILE:
            break

    return sorted(set(added))


def parse_hunks(patch: str | None) -> list[tuple[int, int]]:
    """Return ``(new_start, new_count)`` for every hunk in the patch."""
    if not patch:
        return []

    hunks: list[tuple[int, int]] = []
    for raw_line in patch.splitlines():
        header = _HUNK_HEADER_RE.match(raw_line)
        if header:
            new_start = int(header.group("new_start"))
            new_count = int(header.group("new_count") or 1)
            hunks.append((new_start, new_count))
    return hunks


def lines_intersect(
    line_start: int,
    line_end: int,
    changed_lines: list[int] | set[int],
) -> bool:
    """True when a symbol spanning ``[line_start, line_end]`` was touched."""
    if not changed_lines:
        return False
    if line_start > line_end:
        line_start, line_end = line_end, line_start
    return any(line_start <= line <= line_end for line in changed_lines)
