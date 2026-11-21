"""
FAST Team - Test impact analysis
Determines which tests are affected by code changes.
"""
from __future__ import annotations
import ast
import os
from bisect import bisect_left, bisect_right
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
    source_readers: Optional[Dict[str, Set[int]]] = None
    source_reader_unknown: Set[int] = field(default_factory=set)


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


def _safe_numeric_unary(node: ast.AST) -> bool:
    """Check unary numeric operations without accepting expressions that raise."""
    if not isinstance(node, ast.UnaryOp) or not isinstance(node.operand, ast.Constant):
        return False
    value = node.operand.value
    if isinstance(node.op, (ast.UAdd, ast.USub)):
        return type(value) in (int, float, complex)
    if isinstance(node.op, ast.Invert):
        return type(value) is int
    return False


def _is_hashable_literal(node: ast.AST) -> bool:
    """Whether a literal expression is both safe to evaluate and hashable."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp):
        return _safe_numeric_unary(node)
    if isinstance(node, ast.Tuple):
        return all(_is_hashable_literal(item) for item in node.elts)
    return False


def is_literal_value(node: Optional[ast.AST]) -> bool:
    """Return True only for literal expressions that cannot invoke user code or raise."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp):
        return _safe_numeric_unary(node)
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(is_literal_value(item) for item in node.elts)
    if isinstance(node, ast.Set):
        return all(_is_hashable_literal(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        # A None key represents **mapping unpacking, which can execute arbitrary code.
        return all(
            key is not None and _is_hashable_literal(key) and is_literal_value(value)
            for key, value in zip(node.keys, node.values)
        )
    return False


def is_narrowable_assignment(stmt: Optional[ast.AST]) -> bool:
    """Only narrow plain-name assignments with safe, non-raising literal values."""
    names = get_assigned_names(stmt) if stmt is not None else None
    if not names or any(name.startswith("__") and name.endswith("__") for name in names):
        return False
    if isinstance(stmt, ast.Assign):
        return is_literal_value(stmt.value)
    if isinstance(stmt, ast.AnnAssign):
        # A Name annotation is evaluated at import time and may raise NameError.
        return is_literal_value(stmt.value) and isinstance(stmt.annotation, ast.Constant)
    return False


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
        # Coverage maps are queried repeatedly for small line ranges. Keep
        # sorted line indexes so range lookups do not scan every covered line.
        self._coverage_lines = {
            path: sorted(line_map) for path, line_map in maps.coverage.items()
        }
        self._range_cache: Dict[tuple, FrozenSet[int]] = {}
        self._file_test_cache: Dict[str, FrozenSet[int]] = {}

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
        """Get tests covering line range using a sorted per-file line index."""
        if end_ln < start_ln:
            return set()
        key = (path, start_ln, end_ln)
        cached = self._range_cache.get(key)
        if cached is not None:
            return set(cached)

        cov = self.maps.coverage.get(path, {})
        line_numbers = self._coverage_lines.get(path, ())
        left = bisect_left(line_numbers, start_ln)
        right = bisect_right(line_numbers, end_ln)
        result: Set[int] = set()
        for line_number in line_numbers[left:right]:
            result.update(cov[line_number])
        self._range_cache[key] = frozenset(result)
        return result

    def file_tests(self, path: str) -> Set[int]:
        """Get all tests covering a file, cached after the first query."""
        cached = self._file_test_cache.get(path)
        if cached is None:
            cached = frozenset(
                test_number
                for tests in self.maps.coverage.get(path, {}).values()
                for test_number in tests
            )
            self._file_test_cache[path] = cached
        return set(cached)

    def fixture_tests(self, path: str) -> Optional[Set[int]]:
        """Get tests using a fixture, or ALL when the dependency is unmapped."""
        if path in self.maps.fixtures:
            return set(self.maps.fixtures[path])
        parent = os.path.dirname(path) + "/"
        result: Set[int] = set()
        for fp, tests in self.maps.fixtures.items():
            if fp.startswith(parent):
                result.update(tests)
        return result if result else ALL

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

    def source_reader_tests(self, path: str) -> Optional[Set[int]]:
        """Tests that statically inspect source text for this module."""
        if self.maps.source_readers is None or path not in self.maps.source_readers:
            return self.module_tests(path)
        return set(self.maps.source_readers[path]) | set(self.maps.source_reader_unknown)

    def class_users(self, path: str, classes: List[ast.ClassDef]) -> Set[int]:
        """Get tests that instantiate given classes."""
        if not classes:
            return set()
        names = [c.name for c in classes]
        for depth in range(len(names), 0, -1):
            key = f"{path}::{'.'.join(names[:depth])}"
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
        if not names or not self.maps.import_lines or not is_narrowable_assignment(stmt):
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
            stmts = [n for n in nodes if isinstance(n, ast.stmt)]
            stmt = stmts[-1] if stmts else None
            broad = self.broad_selection(path, classes)
            # For a literal class attribute, tests that instantiate the
            # innermost class are a tighter safe bound than every test that
            # merely imports the containing module.
            if classes and is_narrowable_assignment(stmt):
                class_tests = self.class_users(path, classes)
                if class_tests:
                    broad = class_tests
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
        """Determine tests affected by file change, including renames."""
        paths = {p for p in (diff.old_path, diff.new_path) if p}
        # Binary content cannot be safely analyzed by line/AST maps.
        if diff.binary:
            return ALL
        # Dispatch using both sides before applying suffix-based exclusions.
        if any(p.startswith("tests/fixtures/") for p in paths):
            return self._handle_fixture(diff)
        if any(p.startswith("sqlglot/") for p in paths):
            return self._handle_sqlglot(diff)
        if any(p.startswith("tests/") and p.endswith(".py") for p in paths):
            return self._handle_test(diff)
        # Non-Python test assets outside the fixture map can still be loaded by
        # tests. Unknown test-tree changes therefore require the full suite.
        if any(p.startswith("tests/") for p in paths):
            return ALL
        # Ignore a rename only when both sides are known non-runtime content.
        def ignored(candidate: str) -> bool:
            return candidate.endswith(SKIP_EXTS) or candidate.startswith(SKIP_DIRS)

        if paths and all(ignored(candidate) for candidate in paths):
            return set()
        # Unknown root/package/config files may affect imports or test collection.
        return ALL

    def _handle_fixture(self, diff: FileDiff) -> Optional[Set[int]]:
        """Handle fixture changes, including both sides of a rename."""
        paths = {p for p in (diff.old_path, diff.new_path) if p}
        result: Set[int] = set()
        for candidate in paths:
            affected = self.fixture_tests(candidate)
            if affected is ALL:
                return ALL
            result |= affected
        return result

    def _handle_test(self, diff: FileDiff) -> Optional[Set[int]]:
        """Handle test modules and helpers, including both sides of a rename."""
        result: Set[int] = set()
        for candidate in (diff.old_path, diff.new_path):
            if candidate:
                affected = self.module_tests(candidate)
                if affected is ALL:
                    return ALL
                result |= affected
        return result

    def _handle_sqlglot(self, diff: FileDiff) -> Optional[Set[int]]:
        """Handle sqlglot source changes."""
        path = diff.path

        # New file
        if diff.old_path is None:
            # A new module has no coverage/import map. Existing code may import
            # it dynamically, so an empty selection cannot be proven safe.
            return ALL

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

        # Equal bytecode does not prove a source edit harmless: tests may
        # inspect source text, comments, or formatting. Run tests that exercise
        # this module rather than silently dropping the change.
        if old_key == new_key:
            return self.source_reader_tests(path)

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

        # A bytecode-identical hunk may still alter inspectable source text.
        # Preserve coverage for the whole module for comments/formatting edits.
        try:
            old_source = "\n".join(old_lines)
            patched = apply_hunks(old_lines, [hunk])
            new_source = "\n".join(patched)
            if old_source != new_source and module_key(old_source, path) == module_key(new_source, path):
                return self.source_reader_tests(path)
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
            # The inserted definition is absent from the original source tree.
            # Do not exclude adjacent original lines: they may contain real readers.
            defining = (path, 0, -1)
            # Find matching class in old tree
            classes = [n for n in new_chain if isinstance(n, ast.ClassDef)]
            if classes:
                old_cls = self._find_scope(old_tree, [c.name for c in classes])
                broad = self.broad_selection(path, [old_cls] if old_cls else [])
                if is_narrowable_assignment(stmt):
                    class_tests = self.class_users(path, classes)
                    if class_tests:
                        broad = class_tests
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
