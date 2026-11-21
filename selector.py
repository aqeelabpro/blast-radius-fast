#!/usr/bin/env python3
"""
FAST Team - Test Selection for sqlglot patches
Determines minimal test set needed to catch regressions using coverage analysis.
"""
from __future__ import annotations

import gzip
import json
import os
import pickle
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from br.diff import parse_patch
from br.impact import ALL, Analyzer, Maps
from br.importcheck import ImportChecker
from br.schedule import split

INPUT_DIR = Path(os.environ.get("BR_DATA", "/data"))
SQLGLOT_DIR = os.environ.get("BR_SQLGLOT", "/opt/sqlglot")
MAPS_DIR = Path(os.environ.get("BR_MAPS", str(ROOT / "maps")))


def stderr(msg: str) -> None:
    """Write diagnostic message to stderr."""
    print(msg, file=sys.stderr)


def load_json_map(filename: str):
    """Load a map file, preferring pickled version for speed."""
    base = filename.removesuffix(".gz").removesuffix(".json")
    pkl_path = MAPS_DIR / f"{base}.pickle"

    if pkl_path.exists():
        with open(pkl_path, "rb") as fh:
            return pickle.load(fh)

    json_path = MAPS_DIR / filename
    if not json_path.exists():
        return None

    open_fn = gzip.open if filename.endswith(".gz") else open
    with open_fn(json_path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def build_maps(test_list: list[dict]) -> Maps:
    """Construct Maps object from loaded coverage data."""
    cov_data = load_json_map("coverage.json.gz")

    if (MAPS_DIR / "coverage.pickle").exists():
        cov_map = cov_data
    else:
        cov_map = {
            fpath: {int(line): tests for line, tests in lines.items()}
            for fpath, lines in cov_data.items()
        }

    tests_per_file: dict[str, set[int]] = defaultdict(set)
    for t in test_list:
        tests_per_file[t["file"]].add(t["number"])

    return Maps(
        coverage=cov_map,
        fixtures=load_json_map("fixtures.json") or {},
        tests_by_file=dict(tests_per_file),
        classes=load_json_map("classes.json.gz") or {},
        enums=frozenset(load_json_map("enums.json") or ()),
        import_lines=load_json_map("import_lines.json") or {},
        import_groups=load_json_map("import_groups.json.gz") or {},
    )


def determine_tests(analyzer: Analyzer, patch_content: str, all_tests: set[int],
                    mod_list: list[str], import_checker: ImportChecker) -> set[int]:
    """Analyze patch and return set of tests that could be affected."""
    affected: set[int] = set()
    analyzer.import_time = {}

    for diff in parse_patch(patch_content):
        result = analyzer.tests_for(diff)
        if result is ALL:
            return set(all_tests)
        affected |= result

    if analyzer.import_time and import_checker.crashes(analyzer.import_time):
        stderr("  detected import-time issue, running all tests")
        return set(all_tests)

    return affected & all_tests


def get_collection_modules() -> list[str]:
    """Get module names imported during test collection."""
    imports_file = MAPS_DIR / "collect_imports.json"
    if not imports_file.exists():
        return []

    result = []
    for path in json.loads(imports_file.read_text()):
        mod = path[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[:-9]
        result.append(mod)
    return result


def run():
    """Main entry point."""
    test_data = json.loads((INPUT_DIR / "tests.json").read_text())["tests"]

    runtimes = {t["number"]: round(t["seconds"] * 1000) for t in test_data}
    all_test_nums = set(runtimes)

    analysis = Analyzer(SQLGLOT_DIR, build_maps(test_data))
    modules = get_collection_modules()
    checker = ImportChecker(SQLGLOT_DIR, modules, analysis.maps.import_groups.get("dependents", {}))

    commits_path = INPUT_DIR / "commits"
    for patch_file in sorted(commits_path.glob("*.patch")):
        commit_id = patch_file.stem

        try:
            content = patch_file.read_text(encoding="utf-8", errors="replace")
            selected = determine_tests(analysis, content, all_test_nums, modules, checker)
        except Exception as err:
            stderr(f"{commit_id}: error ({type(err).__name__}: {err}), running all")
            selected = set(all_test_nums)

        worker_assignment = split(selected, runtimes)
        print(json.dumps({"commit": commit_id, "workers": worker_assignment}))
        stderr(f"{commit_id}: {len(selected)} tests")

    checker.close()
    sys.stdout.flush()


if __name__ == "__main__":
    run()
