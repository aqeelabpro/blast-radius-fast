#!/usr/bin/env python3
"""
FAST Team - Blast Radius Test Selector
Selects minimal test set for sqlglot patches using line-level coverage analysis.
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

from analysis.diff import parse_patch
from analysis.impact import ALL, Analyzer, Maps
from analysis.importcheck import ImportChecker
from analysis.schedule import split

# Paths
INPUT_DIR = Path(os.environ.get("BR_DATA", "/data"))
SQLGLOT_DIR = os.environ.get("BR_SQLGLOT", "/opt/sqlglot")
MAPS_DIR = Path(os.environ.get("BR_MAPS", str(ROOT / "maps")))


def log(msg: str):
    """Log to stderr."""
    print(msg, file=sys.stderr)


def load_map(name: str):
    """Load map file, preferring pickle for speed."""
    stem = name.removesuffix(".gz").removesuffix(".json")
    pkl = MAPS_DIR / f"{stem}.pickle"

    if pkl.exists():
        with open(pkl, "rb") as f:
            return pickle.load(f)

    path = MAPS_DIR / name
    if not path.exists():
        return None

    opener = gzip.open if name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def build_maps(tests: list) -> Maps:
    """Build Maps object from loaded data."""
    # Load coverage with int keys
    raw_cov = load_map("coverage.json.gz")
    if (MAPS_DIR / "coverage.pickle").exists():
        coverage = raw_cov
    else:
        coverage = {
            path: {int(ln): nums for ln, nums in lines.items()}
            for path, lines in raw_cov.items()
        }

    # Build test-by-file index
    by_file: dict = defaultdict(set)
    for t in tests:
        by_file[t["file"]].add(t["number"])

    return Maps(
        coverage=coverage,
        fixtures=load_map("fixtures.json") or {},
        tests_by_file=dict(by_file),
        classes=load_map("classes.json.gz") or {},
        enums=frozenset(load_map("enums.json") or ()),
        import_lines=load_map("import_lines.json") or {},
        import_groups=load_map("import_groups.json.gz") or {},
    )


def select_tests(analyzer: Analyzer, patch: str, all_nums: set, checker: ImportChecker) -> set:
    """Analyze patch and return tests to run."""
    selected: set = set()
    analyzer.import_time = {}

    for diff in parse_patch(patch):
        result = analyzer.tests_for(diff)
        if result is ALL:
            return set(all_nums)
        selected |= result

    # Check for import-time crashes
    if analyzer.import_time and checker.crashes(analyzer.import_time):
        log("  import crash detected, running all")
        return set(all_nums)

    return selected & all_nums


def main():
    # Load test data
    tests = json.loads((INPUT_DIR / "tests.json").read_text())["tests"]
    runtimes = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    all_nums = set(runtimes)

    # Initialize
    analyzer = Analyzer(SQLGLOT_DIR, build_maps(tests))
    checker = ImportChecker(SQLGLOT_DIR, [], {})

    # Process patches
    for patch_file in sorted((INPUT_DIR / "commits").glob("*.patch")):
        cid = patch_file.stem

        try:
            content = patch_file.read_text(encoding="utf-8", errors="replace")
            selected = select_tests(analyzer, content, all_nums, checker)
        except Exception as e:
            log(f"{cid}: ERROR {e}, running all")
            selected = set(all_nums)

        workers = split(selected, runtimes)
        print(json.dumps({"commit": cid, "workers": workers}))
        log(f"{cid}: {len(selected)} tests")

    checker.close()
    sys.stdout.flush()


if __name__ == "__main__":
    main()
