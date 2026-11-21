"""Find where a class- or module-level name is read, to narrow constant edits.

For an edit such as `WEEK_OFFSET = -2` inside `class TSQL`, only tests that execute a
statement reading `WEEK_OFFSET` can observe it. This index returns those statements,
or None when the name is read in a way we cannot bound: at import time (metaclasses,
class bodies, module tables), through a string (getattr), or under an import alias.
"""

from __future__ import annotations

import ast
import os
import re

from br.astutil import FUNC_TYPES, chain, own_lines

SOURCE_DIRS = ("sqlglot", "tests")
Site = tuple[str, int, int]


def _mentions(node: ast.AST | None, name: str) -> bool:
    if node is None:
        return False
    return any(
        (isinstance(n, ast.Name) and n.id == name) or (isinstance(n, ast.Attribute) and n.attr == name)
        for n in ast.walk(node)
    )


def _only_defines(stmt: ast.stmt, name: str) -> bool:
    """True when stmt binds `name` as a plain target and never reads it."""
    if isinstance(stmt, ast.Assign):
        targets, value = stmt.targets, stmt.value
    elif isinstance(stmt, ast.AnnAssign):
        targets, value = [stmt.target], stmt.value
    else:
        return False
    binds = any(isinstance(t, ast.Name) and t.id == name for t in targets)
    return binds and not _mentions(value, name)


class ReaderIndex:
    def __init__(self, src_root: str, import_lines: dict[str, list[int]]) -> None:
        self.src_root = src_root
        self.import_lines = {f: set(lines) for f, lines in import_lines.items()}
        self._texts: dict[str, str] | None = None
        self._trees: dict[str, ast.AST] = {}

    def _all_texts(self) -> dict[str, str]:
        if self._texts is None:
            texts: dict[str, str] = {}
            for top in SOURCE_DIRS:
                for folder, _, files in os.walk(os.path.join(self.src_root, top)):
                    for name in files:
                        if name.endswith(".py"):
                            full = os.path.join(folder, name)
                            rel = os.path.relpath(full, self.src_root).replace(os.sep, "/")
                            with open(full, encoding="utf-8") as handle:
                                texts[rel] = handle.read()
            self._texts = texts
        return self._texts

    def _tree(self, path: str) -> ast.AST:
        if path not in self._trees:
            self._trees[path] = ast.parse(self._all_texts()[path])
        return self._trees[path]

    def reader_lines(self, name: str, defining: Site) -> list[Site] | None:
        """Statements (file, start, end) that read `name`, excluding its definition.

        None means "cannot bound": the caller must keep its broad selection.
        """
        word = re.compile(rf"\b{re.escape(name)}\b")
        quoted = re.compile(rf"""["']{re.escape(name)}["']""")
        def_path, def_start, def_end = defining
        out: list[Site] = []
        for path, text in self._all_texts().items():
            if name not in text or not word.search(text):
                continue
            if quoted.search(text):
                return None
            for lineno, line in enumerate(text.split("\n"), start=1):
                if name not in line or not word.search(line) or line.lstrip().startswith("#"):
                    continue
                if path == def_path and def_start <= lineno <= def_end:
                    continue
                site = self._site(path, lineno, name)
                if site is None:
                    return None
                if site:
                    out.append(site)
        return out

    def _site(self, path: str, lineno: int, name: str) -> Site | tuple | None:
        """Runtime statement at lineno; () to ignore (plain imports); None if unbounded."""
        try:
            nodes = chain(self._tree(path), lineno)
        except SyntaxError:
            return None
        stmts = [n for n in nodes if isinstance(n, ast.stmt)]
        if stmts and isinstance(stmts[-1], ast.ImportFrom):
            aliased = any(a.asname for a in stmts[-1].names if a.name == name)
            return None if aliased else ()
        if stmts and isinstance(stmts[-1], ast.Import):
            return ()
        in_function = any(isinstance(n, FUNC_TYPES) for n in nodes)
        if not in_function and stmts and _only_defines(stmts[-1], name):
            return ()  # another class or module binding the same name: a write, not a read
        if not in_function or lineno in self.import_lines.get(path, ()):
            return None
        start, end = own_lines(stmts[-1]) if stmts else (lineno, lineno)
        return path, min(start, lineno), max(end, lineno)
