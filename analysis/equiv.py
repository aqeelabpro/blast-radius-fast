"""
FAST Team - Semantic equivalence checking
Uses bytecode comparison and text canonicalization to detect non-semantic changes.
"""
from __future__ import annotations
import re
import types
from typing import FrozenSet

from analysis.diff import Hunk

# Files where == None has different semantics than is None
UNSAFE_EQ_FILES = ("sqlglot/executor/env.py",)

# Patterns for None comparison normalization
PAT_NONE_EQ_RIGHT = re.compile(r"==\s*None\b")
PAT_NONE_NE_RIGHT = re.compile(r"!=\s*None\b")
PAT_NONE_EQ_LEFT = re.compile(r"\bNone\s*==")
PAT_NONE_NE_LEFT = re.compile(r"\bNone\s*!=")


def _extract_code_signature(code: types.CodeType) -> tuple:
    """Extract comparable signature from code object, recursively handling nested code."""
    nested = tuple(
        _extract_code_signature(c) if isinstance(c, types.CodeType) else (type(c).__name__, repr(c))
        for c in code.co_consts
    )
    return (
        code.co_name, code.co_code, nested, code.co_names, code.co_varnames,
        code.co_freevars, code.co_cellvars, code.co_argcount,
        code.co_posonlyargcount, code.co_kwonlyargcount, code.co_flags,
        code.co_exceptiontable,
    )


def module_key(source, path: str) -> tuple:
    """Get behavioral fingerprint of module via bytecode compilation."""
    return _extract_code_signature(compile(source, path, "exec", dont_inherit=True))


class Equivalence:
    """Checks if changes are semantically equivalent."""

    def __init__(self, enums: FrozenSet[str]):
        # Build patterns for enum identity comparison
        if enums:
            enum_names = "|".join(map(re.escape, sorted(enums)))
            self.pat_enum_eq = re.compile(r"==(\s*)(" + enum_names + r")\.")
            self.pat_enum_ne = re.compile(r"!=(\s*)(" + enum_names + r")\.")
        else:
            self.pat_enum_eq = None
            self.pat_enum_ne = None

    def canonicalize(self, line: str) -> str:
        """Normalize comparison operators to canonical form."""
        # Convert == None to is None
        line = PAT_NONE_EQ_RIGHT.sub("is None", line)
        line = PAT_NONE_NE_RIGHT.sub("is not None", line)
        line = PAT_NONE_EQ_LEFT.sub("None is", line)
        line = PAT_NONE_NE_LEFT.sub("None is not", line)

        # Convert enum == to is
        if self.pat_enum_eq:
            line = self.pat_enum_eq.sub(r"is\1\2.", line)
            line = self.pat_enum_ne.sub(r"is not\1\2.", line)

        # Normalize whitespace
        return " ".join(line.split())

    def same_hunk_text(self, hunk: Hunk, path: str) -> bool:
        """Check if hunk only changes equivalent comparison forms."""
        if path in UNSAFE_EQ_FILES:
            return False

        removed = [ln[1:] for ln in hunk.lines if ln.startswith("-")]
        added = [ln[1:] for ln in hunk.lines if ln.startswith("+")]

        if not removed or len(removed) != len(added):
            return False

        return all(
            self.canonicalize(r) == self.canonicalize(a)
            for r, a in zip(removed, added)
        )
