"""
FAST Team - Unified diff parser
Extracts file changes and line numbers from patches.
"""
from __future__ import annotations
import re
import shlex
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class Hunk:
    """Single hunk from unified diff."""
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: List[str] = field(default_factory=list)

    def removed_lines(self) -> List[int]:
        """Get line numbers of removed lines (old file coordinates)."""
        nums = []
        ln = self.old_start
        for line in self.lines:
            if line.startswith("-"):
                nums.append(ln)
                ln += 1
            elif not line.startswith("+"):
                ln += 1
        return nums

    def added_lines(self) -> List[int]:
        """Get line numbers of added lines (new file coordinates)."""
        nums = []
        ln = self.new_start
        for line in self.lines:
            if line.startswith("+"):
                nums.append(ln)
                ln += 1
            elif not line.startswith("-"):
                ln += 1
        return nums

    def changed_text(self) -> List[str]:
        """Get content of changed lines without +/- prefix."""
        return [line[1:] for line in self.lines if line.startswith(("+", "-"))]


@dataclass
class FileDiff:
    """Changes to a single file."""
    old_path: Optional[str]
    new_path: Optional[str]
    hunks: List[Hunk] = field(default_factory=list)
    binary: bool = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""


def parse_patch(text: str) -> List[FileDiff]:
    """Parse unified diff into list of FileDiff objects."""
    results = []
    current: Optional[FileDiff] = None
    hunk: Optional[Hunk] = None

    for line in text.splitlines():
        if line.startswith("diff --git"):
            if current is not None:
                results.append(current)
            current = FileDiff(None, None)
            hunk = None
            # Rename-only diffs have no ---/+++ pair, so retain header paths.
            try:
                fields = shlex.split(line)
                if len(fields) >= 4:
                    old_path = fields[2][2:] if fields[2].startswith("a/") else fields[2]
                    new_path = fields[3][2:] if fields[3].startswith("b/") else fields[3]
                    current.old_path = old_path
                    current.new_path = new_path
            except ValueError:
                pass

        elif line.startswith("rename from ") and current is not None:
            current.old_path = line[len("rename from "):].strip().strip('"')

        elif line.startswith("rename to ") and current is not None:
            current.new_path = line[len("rename to "):].strip().strip('"')

        elif line.startswith("--- ") and current is not None:
            path = line[4:].split("\t", 1)[0].strip()
            if path.startswith("a/"):
                path = path[2:]
            current.old_path = None if path == "/dev/null" else path

        elif line.startswith("+++ ") and current is not None:
            path = line[4:].split("\t", 1)[0].strip()
            if path.startswith("b/"):
                path = path[2:]
            current.new_path = None if path == "/dev/null" else path

        elif line.startswith("@@") and current is not None:
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                hunk = Hunk(
                    old_start=int(m.group(1)),
                    old_len=int(m.group(2) or 1),
                    new_start=int(m.group(3)),
                    new_len=int(m.group(4) or 1)
                )
                current.hunks.append(hunk)

        elif line.startswith("Binary") and current is not None:
            current.binary = True

        elif hunk is not None and line.startswith(("+", "-", " ")):
            hunk.lines.append(line)

    if current is not None:
        results.append(current)

    return results


def apply_hunks(source_lines: List[str], hunks: List[Hunk]) -> List[str]:
    """Apply hunks to source, return new content."""
    result = list(source_lines)
    offset = 0

    for h in sorted(hunks, key=lambda x: x.old_start):
        # Zero-length old ranges use old_start as the number of preceding lines.
        # Non-empty ranges use the normal 1-based unified-diff line number.
        pos = (h.old_start if h.old_len == 0 else h.old_start - 1) + offset

        # Count removals
        rm_count = sum(1 for ln in h.lines if ln.startswith("-") or ln.startswith(" "))

        # Build new content
        new_content = []
        for ln in h.lines:
            if ln.startswith("+"):
                new_content.append(ln[1:])
            elif ln.startswith(" "):
                new_content.append(ln[1:])

        result[pos:pos + rm_count] = new_content
        offset += len(new_content) - rm_count

    return result
