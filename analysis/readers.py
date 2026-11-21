"""
FAST Team - Track where names are read in the codebase
Used to narrow down tests affected by constant changes.
"""
from __future__ import annotations
import ast
import os
import re
from typing import Dict, List, Optional, Set, Tuple

from analysis.astutil import FUNC_TYPES, chain, own_lines

SOURCE_DIRS = ("sqlglot", "tests")
Site = Tuple[str, int, int]  # (file, start_line, end_line)


def mentions_name(node: Optional[ast.AST], name: str) -> bool:
    """Check if AST node references the given name."""
    if node is None:
        return False
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id == name:
            return True
        if isinstance(n, ast.Attribute) and n.attr == name:
            return True
    return False


def only_defines(stmt: ast.stmt, name: str) -> bool:
    """Check if statement only defines name without reading it."""
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
        value = stmt.value
    elif isinstance(stmt, ast.AnnAssign):
        targets = [stmt.target]
        value = stmt.value
    else:
        return False

    # Check if name is a target
    is_target = any(isinstance(t, ast.Name) and t.id == name for t in targets)
    # Check if name is used in value
    reads_name = mentions_name(value, name)

    return is_target and not reads_name


class ReaderIndex:
    """Index for finding where names are read in the codebase."""

    def __init__(self, src_root: str, import_lines: Dict[str, List[int]]):
        self.src_root = src_root
        self.import_lines = {f: set(lines) for f, lines in import_lines.items()}
        self._source_cache: Optional[Dict[str, str]] = None
        self._tree_cache: Dict[str, ast.AST] = {}

    def _load_sources(self) -> Dict[str, str]:
        """Load all Python source files."""
        if self._source_cache is not None:
            return self._source_cache

        sources: Dict[str, str] = {}
        for top_dir in SOURCE_DIRS:
            base = os.path.join(self.src_root, top_dir)
            if not os.path.isdir(base):
                continue

            for dirpath, _, files in os.walk(base):
                for fname in files:
                    if not fname.endswith(".py"):
                        continue
                    full = os.path.join(dirpath, fname)
                    rel = os.path.relpath(full, self.src_root).replace(os.sep, "/")
                    try:
                        with open(full, encoding="utf-8") as f:
                            sources[rel] = f.read()
                    except Exception:
                        pass

        self._source_cache = sources
        return sources

    def _get_tree(self, path: str) -> ast.AST:
        """Get parsed AST for a file."""
        if path not in self._tree_cache:
            self._tree_cache[path] = ast.parse(self._load_sources()[path])
        return self._tree_cache[path]

    def reader_lines(self, name: str, defining: Site) -> Optional[List[Site]]:
        """
        Find statements that read the given name, excluding its definition.
        Returns None if we can't bound the readers (must use broader selection).
        """
        word_pat = re.compile(rf"\b{re.escape(name)}\b")
        quoted_pat = re.compile(rf"""["']{re.escape(name)}["']""")
        def_path, def_start, def_end = defining

        results: List[Site] = []

        for path, source in self._load_sources().items():
            # Quick check: name not in file
            if name not in source or not word_pat.search(source):
                continue

            # If name appears in quotes, we can't track it
            if quoted_pat.search(source):
                return None

            lines = source.split("\n")
            for lineno, line in enumerate(lines, start=1):
                if name not in line:
                    continue
                if not word_pat.search(line):
                    continue
                if line.lstrip().startswith("#"):
                    continue

                # Skip the defining location
                if path == def_path and def_start <= lineno <= def_end:
                    continue

                site = self._classify_site(path, lineno, name)
                if site is None:
                    return None  # Can't bound
                if site:  # Non-empty tuple means it's a valid read site
                    results.append(site)

        return results

    def _classify_site(self, path: str, lineno: int, name: str) -> Optional[Site]:
        """
        Classify a line that mentions the name.
        Returns Site for runtime reads, () for imports/definitions, None if unbounded.
        """
        try:
            nodes = chain(self._get_tree(path), lineno)
        except SyntaxError:
            return None

        stmts = [n for n in nodes if isinstance(n, ast.stmt)]

        # ImportFrom: check for aliasing
        if stmts and isinstance(stmts[-1], ast.ImportFrom):
            aliased = any(a.asname for a in stmts[-1].names if a.name == name)
            return None if aliased else ()

        # Plain Import
        if stmts and isinstance(stmts[-1], ast.Import):
            return ()

        # Check if inside a function
        in_function = any(isinstance(n, FUNC_TYPES) for n in nodes)

        # If not in function and only defines the name, it's a write not a read
        if not in_function and stmts and only_defines(stmts[-1], name):
            return ()

        # If not in function or at import time, can't bound
        if not in_function or lineno in self.import_lines.get(path, ()):
            return None

        # Return the statement's line range
        if stmts:
            s, e = own_lines(stmts[-1])
            return path, min(s, lineno), max(e, lineno)
        return path, lineno, lineno
