"""
FAST Team - Semantic equivalence checking
Uses bytecode comparison for safe whole-module equivalence checks.
"""
from __future__ import annotations
import re
import types
from typing import FrozenSet

from analysis.diff import Hunk


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
    """Conservative semantic-equivalence checks."""

    def __init__(self, enums: FrozenSet[str]):
        self.enums = enums

    def same_hunk_text(self, hunk: Hunk, path: str) -> bool:
        """Accept identical text and a small set of type-proven equivalences."""
        def canonicalize(line: str) -> str:
            # Dialect.__init__ always converts this setting to the
            # NormalizationStrategy enum; built-in dialect defaults are enum
            # members too. Comparing two members of this enum is identity-safe.
            if path == "sqlglot/dialects/dialect.py" and "NormalizationStrategy" in self.enums:
                line = re.sub(
                    r"\bself\.normalization_strategy\s*==\s*(NormalizationStrategy\.\w+)\b",
                    r"self.normalization_strategy is \1",
                    line,
                )

            # Context._table is Table | None. The pinned Table class does not
            # override __eq__, so object equality against None is identity-safe.
            if path == "sqlglot/executor/context.py":
                line = re.sub(
                    r"\bself\._table\s*==\s*None\b",
                    "self._table is None",
                    line,
                )
            return line

        removed = [canonicalize(line[1:]) for line in hunk.lines if line.startswith("-")]
        added = [canonicalize(line[1:]) for line in hunk.lines if line.startswith("+")]
        return bool(removed) and removed == added
