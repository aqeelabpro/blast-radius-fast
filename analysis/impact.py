"""
FAST Team - Test impact analysis
Determines which tests are affected by code changes.
"""
from __future__ import annotations
import ast
import os
from dataclasses import dataclass, field
from typing import Dict, List, Set, Optional, FrozenSet, Union

from analysis.astutil import chain, own_lines, start, FUNC_TYPES
from analysis.diff import FileDiff, Hunk, apply_hunks
from analysis.equiv import Equivalence, module_key
from analysis.readers import ReaderIndex

# Sentinel for "run all tests"
ALL = None

SKIP_EXTS = (".md", ".rst", ".txt", ".png", ".svg", ".html", ".jpg", ".gif")
SKIP_DIRS = ("docs/", ".github/", "benchmarks/", "posts/")
DIALECT_DIRS = ("dialects", "parsers", "generators", "typing")
DIALECT_SKIP = ("__init__", "dialect", "base")
TYPING_PKG = "sqlglot/typing/"
TYPING_TEST = "tests/test_typing.py"


@dataclass
class Maps:
    """Pre-built coverage and mapping data."""
    coverage: Dict[str, Dict[int, List[int]]]
    fixtures: Dict[str, List[int]]
    tests_by_file: Dict[str, Set[int]]
    classes: Dict[str, List[int]] = field(default_factory=dict)
    enums: FrozenSet[str] = frozenset()
    import_lines: Dict[str, List[int]] = field(default_factory=dict)
    import_groups: Dict = field(default_factory=dict)


def union_results(parts) -> Optional[Set[int]]:
    """Union multiple test sets; None if any is ALL."""
    result: Set[int] = set()
    for p in parts:
        if p is ALL:
            return ALL
        result |= p
    return result


def is_code_line(text: str) -> bool:
    """Check if line contains code (not empty/comment)."""
    stripped = text.strip()
    return bool(stripped) and not stripped.startswith("#")


def find_next_code_line(lines: List[str], idx: int, direction: int) -> Optional[int]:
    """Find nearest code line from idx in given direction."""
    while 1 <= idx <= len(lines):
        if is_code_line(lines[idx - 1]):
            return idx
        idx += direction
    return None


def get_assigned_names(stmt: ast.AST) -> Optional[List[str]]:
    """Get names assigned by a statement, or None for non-assignments."""
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, ast.AnnAssign):
        targets = [stmt.target]
    elif isinstance(stmt, ast.AugAssign):
        targets = [stmt.target]
    else:
        return None

    names = []
    for t in targets:
        if not isinstance(t, ast.Name):
            return None
        names.append(t.id)
    return names or None


class Analyzer:
    """Analyzes patches to determine affected tests."""

    def __init__(self, src_root: str, maps: Maps):
        self.src = src_root
        self.maps = maps
        self.equiv = Equivalence(maps.enums)
        self.readers = ReaderIndex(src_root, maps.import_lines)
        self.import_time: Dict[str, str] = {}

    def read_source(self, path: str) -> Optional[str]:
        """Read file from source tree."""
        full = os.path.join(self.src, path)
        if os.path.isfile(full):
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    return f.read()
            except Exception:
                pass
        return None

    def line_tests(self, path: str, lines) -> Set[int]:
        """Get tests covering given lines."""
        cov = self.maps.coverage.get(path, {})
        result: Set[int] = set()
        for ln in lines:
            result.update(cov.get(ln, []))
        return result

    def range_tests(self, path: str, start_ln: int, end_ln: int) -> Set[int]:
        """Get tests covering line range."""
        cov = self.maps.coverage.get(path, {})
        result: Set[int] = set()
        for ln, tests in cov.items():
            if start_ln <= ln <= end_ln:
                result.update(tests)
        return result

    def file_tests(self, path: str) -> Set[int]:
        """Get all tests covering file."""
        return self.range_tests(path, 0, 10**9)

    def fixture_tests(self, path: str) -> Set[int]:
        """Get tests using fixture."""
        if path in self.maps.fixtures:
            return set(self.maps.fixtures[path])
        parent = os.path.dirname(path) + "/"
        result: Set[int] = set()
        for fp, tests in self.maps.fixtures.items():
            if fp.startswith(parent):
                result.update(tests)
        return result

    def module_tests(self, path: str) -> Optional[Set[int]]:
        """Get tests for a module, including dialect family."""
        tests = self.file_tests(path) | self.maps.tests_by_file.get(path, set())

        parts = path.split("/")
        if len(parts) == 3 and parts[0] == "sqlglot" and parts[1] in DIALECT_DIRS:
            name = parts[2][:-3]
            if name not in DIALECT_SKIP:
                for subdir in DIALECT_DIRS:
                    tests |= self.range_tests(f"sqlglot/{subdir}/{name}.py", 0, 10**9)
                tests |= self.maps.tests_by_file.get(f"tests/dialects/test_{name}.py", set())

        return tests or ALL

    def class_users(self, path: str, classes: List[ast.ClassDef]) -> Set[int]:
        """Get tests that instantiate given classes."""
        if not classes:
            return set()
        names = [c.name for c in classes]
        for depth in range(len(names), 0, -1):
            key = f"{path}::{'::'.join(names[:depth])}"
            if key in self.maps.classes:
                return set(self.maps.classes[key])
        return set()

    def import_group_tests(self, path: str, lines) -> Optional[Set[int]]:
        """Get tests affected via import groups."""
        line_map = self.maps.import_groups.get("lines", {}).get(path, {})
        groups = self.maps.import_groups.get("groups", {})
        result: Set[int] = set()
        for ln in lines:
            for gid in line_map.get(str(ln), []):
                group = groups.get(str(gid))
                if group == "ALL":
                    return ALL
                if group:
                    result.update(group)
        return result

    def broad_selection(self, path: str, classes: List[ast.AST]) -> Optional[Set[int]]:
        """Get broad test selection for import-time changes."""
        if path.startswith("tests/") and path in self.maps.tests_by_file:
            return set(self.maps.tests_by_file[path])

        class_users = self.class_users(path, [c for c in classes if isinstance(c, ast.ClassDef)])
        in_class = set()
        if classes:
            innermost = classes[-1]
            in_class = self.range_tests(path, start(innermost), innermost.end_lineno)

        # Expression classes need special handling
        if classes and path.startswith("sqlglot/expressions/") and (class_users or in_class):
            return class_users | in_class

        mod_tests = self.module_tests(path)
        if mod_tests is ALL or not classes:
            return mod_tests

        is_dialect = len(path.split("/")) == 3 and path.split("/")[1] in DIALECT_DIRS
        if is_dialect:
            return in_class | mod_tests | class_users

        return (in_class | class_users) if (in_class or class_users) else mod_tests

    def narrow_selection(self, path: str, stmt: Optional[ast.AST], defining, broad: Optional[Set[int]]) -> Optional[Set[int]]:
        """Narrow broad selection using reader analysis."""
        names = get_assigned_names(stmt) if stmt else None
        if broad is ALL:
            return ALL
        if not names or not self.maps.import_lines:
            return broad

        readers: Set[int] = set()
        for name in names:
            sites = self.readers.reader_lines(name, defining)
            if sites is None:
                return broad
            for site_path, s, e in sites:
                readers |= self.range_tests(site_path, s, e)
        return broad & readers

    def old_line_tests(self, path: str, tree: ast.AST, line: int) -> Optional[Set[int]]:
        """Get tests affected by change to old line."""
        nodes = chain(tree, line)
        funcs = [i for i, n in enumerate(nodes) if isinstance(n, FUNC_TYPES)]

        # Not in function: import-time code
        if not funcs:
            classes = [n for n in nodes if isinstance(n, ast.ClassDef)]
            broad = self.broad_selection(path, classes)
            stmts = [n for n in nodes if isinstance(n, ast.stmt)]
            stmt = stmts[-1] if stmts else None
            defining = (path, start(stmt), stmt.end_lineno) if stmt else (path, 0, -1)
            return self.narrow_selection(path, stmt, defining, broad)

        # Inside function
        idx = funcs[-1]
        func = nodes[idx]

        # Lambda: use its range
        if isinstance(func, ast.Lambda):
            return self.range_tests(path, func.lineno, func.end_lineno)

        # In decorator/signature (import-time)
        if line < func.body[0].lineno:
            return self.range_tests(path, start(func), func.end_lineno)

        # In function body: use statement coverage
        stmts = [n for n in nodes[idx + 1:] if isinstance(n, ast.stmt)]
        if not stmts:
            return self.line_tests(path, {line})

        s, e = own_lines(stmts[-1])
        s, e = min(s, line), max(e, line)
        result = self.range_tests(path, s, e)
        ig = self.import_group_tests(path, range(s, e + 1))
        return union_results([result, ig])

    def tests_for(self, diff: FileDiff) -> Optional[Set[int]]:
        """Determine tests affected by file change."""
        path = diff.path

        # Skip docs
        if any(path.endswith(ext) for ext in SKIP_EXTS):
            return set()
        if any(path.startswith(d) for d in SKIP_DIRS):
            return set()

        # Fixtures
        if path.startswith("tests/fixtures/"):
            return self._handle_fixture(diff)

        # Python
        if path.endswith(".py"):
            if path.startswith("sqlglot/"):
                return self._handle_sqlglot(diff)
            if path.startswith("tests/"):
                return self._handle_test(diff)

        # Binary
        if diff.binary:
            return ALL
        return set()

    def _handle_fixture(self, diff: FileDiff) -> Set[int]:
        """Handle fixture changes."""
        path = diff.path
        if path.endswith(".sql") and not diff.binary:
            has_code = False
            for hunk in diff.hunks:
                for text in hunk.changed_text():
                    stripped = text.strip()
                    if stripped and not stripped.startswith("--"):
                        has_code = True
                        break
            if not has_code:
                return set()
        return self.fixture_tests(path)

    def _handle_test(self, diff: FileDiff) -> Set[int]:
        """Handle test file changes."""
        return self.maps.tests_by_file.get(diff.path, set())

    def _handle_sqlglot(self, diff: FileDiff) -> Optional[Set[int]]:
        """Handle sqlglot source changes."""
        path = diff.path

        # New file
        if diff.old_path is None:
            extra = self.maps.tests_by_file.get(TYPING_TEST, set()) if path.startswith(TYPING_PKG) else set()
            return extra

        # Deleted or renamed
        if diff.new_path is None or diff.old_path != diff.new_path:
            return ALL

        # Read original
        old_src = self.read_source(path)
        if old_src is None:
            return ALL

        old_lines = old_src.split("\n")

        # Apply patch
        try:
            new_lines = apply_hunks(old_lines, diff.hunks)
            new_src = "\n".join(new_lines)
        except Exception:
            return ALL

        # Compare bytecode
        try:
            old_key = module_key(old_src, path)
            new_key = module_key(new_src, path)
        except SyntaxError:
            self.import_time[path] = new_src
            return ALL

        # Same bytecode or all hunks are equivalent
        if old_key == new_key:
            return set()
        if all(self.equiv.same_hunk_text(h, path) for h in diff.hunks):
            return set()

        # Parse trees
        try:
            old_tree = ast.parse(old_src)
        except SyntaxError:
            return ALL

        # Analyze hunks
        affected: Set[int] = set()
        for hunk in diff.hunks:
            result = self._hunk_tests(path, old_tree, old_lines, hunk, new_lines)
            if result is ALL:
                return ALL
            affected |= result

        # Track import-time changes
        if self.import_time.get(path):
            pass  # Already tracked
        elif any(self._touches_import_time(path, old_tree, h) for h in diff.hunks):
            self.import_time[path] = new_src

        # Extra tests for typing package
        if path.startswith(TYPING_PKG):
            affected |= self.maps.tests_by_file.get(TYPING_TEST, set())

        return affected

    def _touches_import_time(self, path: str, tree: ast.AST, hunk: Hunk) -> bool:
        """Check if hunk modifies import-time code."""
        import_lines = set(self.maps.import_lines.get(path, []))
        for ln in hunk.removed_lines():
            if ln in import_lines:
                return True
            nodes = chain(tree, ln)
            if not any(isinstance(n, FUNC_TYPES) for n in nodes):
                return True
        return False

    def _hunk_tests(self, path: str, old_tree: ast.AST, old_lines: List[str],
                   hunk: Hunk, new_lines: List[str]) -> Optional[Set[int]]:
        """Get tests affected by a single hunk."""
        if self.equiv.same_hunk_text(hunk, path):
            return set()

        # Try new bytecode comparison
        try:
            old_key = module_key("\n".join(old_lines), path)
            patched = apply_hunks(old_lines, [hunk])
            new_key = module_key("\n".join(patched), path)
            if old_key == new_key:
                return set()
        except (SyntaxError, ValueError):
            pass

        # Analyze removed lines
        parts = [self.old_line_tests(path, old_tree, ln) for ln in hunk.removed_lines()]

        # Analyze added lines - need new tree for proper context
        try:
            new_tree = ast.parse("\n".join(new_lines))
        except SyntaxError:
            new_tree = None

        for point, new_ln in self._added_positions(hunk):
            if is_code_line(new_lines[new_ln - 1]):
                if new_tree:
                    parts.append(self._insertion_tests(path, old_tree, old_lines, new_tree, new_ln, point))
                else:
                    # Fallback: use neighbors
                    neighbors = [
                        find_next_code_line(old_lines, point - 1, -1),
                        find_next_code_line(old_lines, point, 1)
                    ]
                    parts.extend(
                        self.old_line_tests(path, old_tree, n)
                        for n in neighbors if n is not None
                    )

        return union_results(parts)

    def _insertion_tests(self, path: str, old_tree: ast.AST, old_lines: List[str],
                         new_tree: ast.AST, new_ln: int, point: int) -> Optional[Set[int]]:
        """Get tests affected by an inserted line."""
        new_chain = chain(new_tree, new_ln)
        funcs = [n for n in new_chain if isinstance(n, FUNC_TYPES)]

        # Not in function: import-time insertion
        if not funcs:
            stmts = [n for n in new_chain if isinstance(n, ast.stmt)]
            stmt = stmts[-1] if stmts else None
            defining = (path, max(point - 1, 1), point)
            # Find matching class in old tree
            classes = [n for n in new_chain if isinstance(n, ast.ClassDef)]
            if classes:
                old_cls = self._find_scope(old_tree, [c.name for c in classes])
                broad = self.broad_selection(path, [old_cls] if old_cls else [])
            else:
                broad = self.broad_selection(path, [])
            return self.narrow_selection(path, stmt, defining, broad)

        # In named function: check if it exists in old tree
        named = [n for n in new_chain if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        if named:
            qual = [n.name for n in new_chain if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
            old_func = self._find_scope(old_tree, qual)
            if old_func is None:
                # New function: use broad selection
                classes = [n for n in new_chain if isinstance(n, ast.ClassDef)]
                if classes:
                    old_cls = self._find_scope(old_tree, [c.name for c in classes])
                    return self.broad_selection(path, [old_cls] if old_cls else [])
                return self.broad_selection(path, [])
            # In decorator/signature
            if isinstance(named[-1], (ast.FunctionDef, ast.AsyncFunctionDef)):
                if new_ln < named[-1].body[0].lineno:
                    return self.range_tests(path, start(old_func), old_func.end_lineno)

        # In function body: use neighbor coverage
        neighbors = [
            find_next_code_line(old_lines, point - 1, -1),
            find_next_code_line(old_lines, point, 1)
        ]
        return union_results([
            self.old_line_tests(path, old_tree, n)
            for n in neighbors if n is not None
        ])

    def _find_scope(self, tree: ast.AST, names: List[str]) -> Optional[ast.AST]:
        """Find a nested scope by qualified name."""
        if not names:
            return None
        current = tree
        for name in names:
            found = None
            for child in ast.iter_child_nodes(current):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if child.name == name:
                        found = child
                        break
            if found is None:
                return None
            current = found
        return current

    def _added_positions(self, hunk: Hunk) -> List[tuple]:
        """Get (old_insertion_point, new_line_number) for added lines."""
        result = []
        old_cursor = hunk.old_start if hunk.old_len else hunk.old_start + 1
        new_cursor = old_cursor

        for line in hunk.lines:
            if line.startswith("+"):
                result.append((old_cursor, new_cursor))
                new_cursor += 1
            elif line.startswith("-"):
                old_cursor += 1
            else:
                old_cursor += 1
                new_cursor += 1

        return result
