import tempfile
import unittest
from pathlib import Path

from analysis.diff import Hunk, apply_hunks, parse_patch
from analysis.impact import ALL, Analyzer, Maps
from analysis.equiv import Equivalence
from analysis.readers import ReaderIndex
from analysis.schedule import split


class ScheduleRegressionTests(unittest.TestCase):
    def test_free_tests_fill_slack_without_increasing_makespan(self):
        runtimes = {1: 8000, 2: 4000, 3: 3000, 4: 700, 5: 300, 6: 2500}
        files = {
            1: "tests/test_a.py",
            2: "tests/test_b.py",
            3: "tests/test_b.py",
            4: "tests/test_b.py",
            5: "tests/test_c.py",
            6: "tests/test_d.py",
        }
        selected = {1, 2, 3}
        base = split(selected, runtimes)
        augmented = split(selected, runtimes, set(runtimes), files)

        base_peak = max(sum(runtimes[n] for n in worker) for worker in base)
        augmented_peak = max(sum(runtimes[n] for n in worker) for worker in augmented)
        flattened = [n for worker in augmented for n in worker]

        self.assertEqual(augmented_peak, base_peak)
        self.assertEqual(len(flattened), len(set(flattened)))
        self.assertTrue(selected.issubset(set(flattened)))
        self.assertIn(4, flattened)
        self.assertIn(5, flattened)
        self.assertNotIn(6, flattened)

    def test_empty_required_selection_stays_empty(self):
        self.assertEqual(split(set(), {1: 100}, {1}), [])


class DiffRegressionTests(unittest.TestCase):
    def test_insert_at_start(self):
        hunk = Hunk(0, 0, 1, 1, ["+first"])
        self.assertEqual(apply_hunks(["second"], [hunk]), ["first", "second"])

    def test_insert_at_end(self):
        hunk = Hunk(2, 0, 3, 1, ["+third"])
        self.assertEqual(apply_hunks(["first", "second"], [hunk]), ["first", "second", "third"])

    def test_rename_only_paths_are_parsed(self):
        patch = (
            "diff --git a/tests/fixtures/old.sql b/tests/fixtures/new.sql\n"
            "similarity index 100%\n"
            "rename from tests/fixtures/old.sql\n"
            "rename to tests/fixtures/new.sql\n"
        )
        diff = parse_patch(patch)[0]
        self.assertEqual(diff.old_path, "tests/fixtures/old.sql")
        self.assertEqual(diff.new_path, "tests/fixtures/new.sql")

    def test_deleted_file_path_is_preserved(self):
        patch = (
            "diff --git a/sqlglot/old.py b/sqlglot/old.py\n"
            "deleted file mode 100644\n"
            "--- a/sqlglot/old.py\n"
            "+++ /dev/null\n"
        )
        diff = parse_patch(patch)[0]
        self.assertEqual(diff.old_path, "sqlglot/old.py")
        self.assertIsNone(diff.new_path)

    def test_quoted_paths_with_spaces(self):
        patch = (
            'diff --git "a/tests/fixtures/old file.sql" "b/tests/fixtures/new file.sql"\n'
            "similarity index 100%\n"
            'rename from "tests/fixtures/old file.sql"\n'
            'rename to "tests/fixtures/new file.sql"\n'
        )
        diff = parse_patch(patch)[0]
        self.assertEqual(diff.old_path, "tests/fixtures/old file.sql")
        self.assertEqual(diff.new_path, "tests/fixtures/new file.sql")


class EquivalenceRegressionTests(unittest.TestCase):
    def test_none_equality_is_not_assumed_equivalent_to_identity(self):
        hunk = Hunk(1, 1, 1, 1, ["-if value == None:", "+if value is None:"])
        self.assertFalse(Equivalence(frozenset()).same_hunk_text(hunk, "sqlglot/example.py"))

    def test_string_literal_whitespace_is_not_normalized(self):
        hunk = Hunk(1, 1, 1, 1, ['-value = "a  b"', '+value = "a b"'])
        self.assertFalse(Equivalence(frozenset()).same_hunk_text(hunk, "sqlglot/example.py"))

    def test_enum_equality_is_not_assumed_to_be_identity(self):
        hunk = Hunk(1, 1, 1, 1, [
            "-if value == NormalizationStrategy.DEFAULT:",
            "+if value is NormalizationStrategy.DEFAULT:",
        ])
        self.assertFalse(Equivalence(frozenset({"NormalizationStrategy"})).same_hunk_text(hunk, "sqlglot/example.py"))

    def test_proven_normalization_strategy_enum_comparison_is_equivalent(self):
        hunk = Hunk(1, 1, 1, 1, [
            "-if self.normalization_strategy is NormalizationStrategy.UPPERCASE:",
            "+if self.normalization_strategy == NormalizationStrategy.UPPERCASE:",
        ])
        self.assertTrue(Equivalence(frozenset({"NormalizationStrategy"})).same_hunk_text(hunk, "sqlglot/dialects/dialect.py"))

    def test_proven_optional_table_none_comparison_is_equivalent(self):
        hunk = Hunk(1, 1, 1, 1, [
            "-if self._table is None:",
            "+if self._table == None:",
        ])
        self.assertTrue(Equivalence(frozenset()).same_hunk_text(hunk, "sqlglot/executor/context.py"))


class ReaderIndexRegressionTests(unittest.TestCase):
    def test_comment_mentions_do_not_make_reader_analysis_unbounded(self):
        with tempfile.TemporaryDirectory() as root:
            source_dir = Path(root) / "sqlglot"
            source_dir.mkdir()
            (source_dir / "module.py").write_text(
                "VALUE = 1\n# VALUE is mentioned in this comment\n",
                encoding="utf-8",
            )
            readers = ReaderIndex(root, {})
            self.assertEqual(readers.reader_lines("VALUE", ("sqlglot/module.py", 1, 1)), [])

    def test_dynamic_string_reader_is_found(self):
        with tempfile.TemporaryDirectory() as root:
            source_dir = Path(root) / "sqlglot"
            source_dir.mkdir()
            (source_dir / "module.py").write_text(
                "VALUE = 1\ndef read(obj):\n    return getattr(obj, \"VALUE\")\n",
                encoding="utf-8",
            )
            readers = ReaderIndex(root, {})
            self.assertEqual(
                readers.reader_lines("VALUE", ("sqlglot/module.py", 1, 1)),
                [("sqlglot/module.py", 3, 3)],
            )

    def test_unbounded_module_level_reader_keeps_broad_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            source_dir = Path(root) / "sqlglot"
            source_dir.mkdir()
            (source_dir / "module.py").write_text(
                "VALUE = 1\nOTHER = VALUE\n",
                encoding="utf-8",
            )
            readers = ReaderIndex(root, {})
            self.assertIsNone(readers.reader_lines("VALUE", ("sqlglot/module.py", 1, 1)))


class ImpactRegressionTests(unittest.TestCase):
    def make_analyzer(self, root):
        maps = Maps(
            coverage={},
            fixtures={
                "tests/fixtures/old.sql": [1, 2],
                "tests/fixtures/new.sql": [3],
            },
            tests_by_file={
                "tests/test_old.py": {4},
                "tests/test_new.py": {5},
            },
        )
        return Analyzer(root, maps)

    def test_fixture_rename_uses_both_paths(self):
        with tempfile.TemporaryDirectory() as root:
            analyzer = self.make_analyzer(root)
            diff = parse_patch(
                "diff --git a/tests/fixtures/old.sql b/tests/fixtures/new.sql\n"
                "similarity index 100%\n"
                "rename from tests/fixtures/old.sql\n"
                "rename to tests/fixtures/new.sql\n"
            )[0]
            self.assertEqual(analyzer.tests_for(diff), {1, 2, 3})

    def test_new_python_module_is_conservative(self):
        with tempfile.TemporaryDirectory() as root:
            analyzer = self.make_analyzer(root)
            diff = parse_patch(
                "diff --git a/sqlglot/new_module.py b/sqlglot/new_module.py\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                "+++ b/sqlglot/new_module.py\n"
                "@@ -0,0 +1 @@\n"
                "+VALUE = 1\n"
            )[0]
            self.assertIs(analyzer.tests_for(diff), ALL)

    def test_range_queries_use_inclusive_line_bounds(self):
        with tempfile.TemporaryDirectory() as root:
            maps = Maps(
                coverage={"sqlglot/example.py": {2: [1], 5: [2], 9: [3]}},
                fixtures={},
                tests_by_file={},
            )
            analyzer = Analyzer(root, maps)
            self.assertEqual(analyzer.range_tests("sqlglot/example.py", 3, 8), {2})
            self.assertEqual(analyzer.range_tests("sqlglot/example.py", 2, 5), {1, 2})
            self.assertEqual(analyzer.file_tests("sqlglot/example.py"), {1, 2, 3})

    def test_side_effectful_module_assignment_is_not_narrowed(self):
        import ast
        with tempfile.TemporaryDirectory() as root:
            maps = Maps(
                coverage={},
                fixtures={},
                tests_by_file={},
                import_lines={"sqlglot/example.py": [1]},
            )
            analyzer = Analyzer(root, maps)
            stmt = ast.parse("REGISTRY = register_adapter()").body[0]
            self.assertEqual(
                analyzer.narrow_selection("sqlglot/example.py", stmt, ("sqlglot/example.py", 1, 1), {1, 2}),
                {1, 2},
            )

    def test_literal_module_assignment_can_be_narrowed(self):
        import ast
        with tempfile.TemporaryDirectory() as root:
            maps = Maps(
                coverage={},
                fixtures={},
                tests_by_file={},
                import_lines={"sqlglot/example.py": [1]},
            )
            analyzer = Analyzer(root, maps)
            stmt = ast.parse("SIMPLE_CONSTANT = 42").body[0]
            self.assertEqual(
                analyzer.narrow_selection("sqlglot/example.py", stmt, ("sqlglot/example.py", 1, 1), {1, 2}),
                set(),
            )

    def test_nested_class_instantiation_map_uses_dotted_class_path(self):
        import ast
        with tempfile.TemporaryDirectory() as root:
            maps = Maps(
                coverage={},
                fixtures={},
                tests_by_file={},
                classes={
                    "sqlglot/example.py::Outer": [1, 2],
                    "sqlglot/example.py::Outer.Inner": [3, 4],
                },
            )
            analyzer = Analyzer(root, maps)
            tree = ast.parse("class Outer:\n    class Inner:\n        VALUE = 1\n")
            outer = tree.body[0]
            inner = outer.body[0]
            self.assertEqual(analyzer.class_users("sqlglot/example.py", [outer, inner]), {3, 4})

    def test_negative_numeric_assignment_is_narrowable(self):
        import ast
        from analysis.impact import is_narrowable_assignment
        stmt = ast.parse("WEEK_OFFSET = -1").body[0]
        self.assertTrue(is_narrowable_assignment(stmt))


    def test_source_reader_map_selects_only_known_source_inspection_tests(self):
        with tempfile.TemporaryDirectory() as root:
            maps = Maps(
                coverage={"sqlglot/example.py": {1: [1, 2, 3]}},
                fixtures={},
                tests_by_file={},
                source_readers={"sqlglot/example.py": {7}},
                source_reader_unknown={8},
            )
            analyzer = Analyzer(root, maps)
            self.assertEqual(analyzer.source_reader_tests("sqlglot/example.py"), {7, 8})

    def test_unmapped_source_file_uses_module_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            maps = Maps(
                coverage={"sqlglot/example.py": {1: [1, 2, 3]}},
                fixtures={},
                tests_by_file={},
                source_readers={"sqlglot/other.py": set()},
            )
            analyzer = Analyzer(root, maps)
            self.assertEqual(analyzer.source_reader_tests("sqlglot/example.py"), {1, 2, 3})


    def test_comment_only_source_change_keeps_module_tests(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            source_dir = Path(root) / "sqlglot"
            source_dir.mkdir()
            (source_dir / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
            maps = Maps(
                coverage={"sqlglot/example.py": {1: [7]}},
                fixtures={},
                tests_by_file={},
            )
            analyzer = Analyzer(root, maps)
            patch = (
                "diff --git a/sqlglot/example.py b/sqlglot/example.py\n"
                "--- a/sqlglot/example.py\n"
                "+++ b/sqlglot/example.py\n"
                "@@ -1 +1,2 @@\n"
                " VALUE = 1\n"
                "+# source comment changed\n"
            )
            self.assertEqual(analyzer.tests_for(parse_patch(patch)[0]), {7})


if __name__ == "__main__":
    unittest.main()
