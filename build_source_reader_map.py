#!/usr/bin/env python3
"""Build a conservative map of tests that inspect sqlglot source text.

This is derived only from the pinned repository's own test sources. It does not
contain patch-to-test answers or mappings by edit category.
"""
from __future__ import annotations

import ast
import pickle
import sys
from pathlib import Path

SOURCE_CALLS = {
    "getsource",
    "getsourcelines",
    "getsourcefile",
    "getfile",
    "getline",
    "getlines",
}


def dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def module_paths(root: Path, module: str) -> set[str]:
    if not module.startswith("sqlglot"):
        return set()
    relative = module.replace(".", "/")
    candidates = {
        root / f"{relative}.py",
        root / relative / "__init__.py",
    }
    found = {p.relative_to(root).as_posix() for p in candidates if p.is_file()}
    if found:
        return found
    package_dir = root / relative
    if package_dir.is_dir():
        return {
            p.relative_to(root).as_posix()
            for p in package_dir.rglob("*.py")
        }
    return set()


def resolve_module_paths(root: Path, module: str) -> set[str]:
    """Resolve an imported object to its defining module or package subtree."""
    candidate = module
    while candidate.startswith("sqlglot"):
        paths = module_paths(root, candidate)
        if paths:
            return paths
        if "." not in candidate:
            break
        candidate = candidate.rsplit(".", 1)[0]
    return set()


def scan_test(path: Path, root: Path) -> tuple[set[str], bool]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return set(), True

    imports: dict[str, str] = {}
    source_functions: set[str] = set()
    source_modules = {"inspect", "linecache"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                imports[bound] = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in source_modules:
                for alias in node.names:
                    if alias.name in SOURCE_CALLS:
                        source_functions.add(alias.asname or alias.name)
            if module.startswith("sqlglot"):
                for alias in node.names:
                    if alias.name == "*":
                        imports[alias.asname or "*"] = module + ".*"
                    else:
                        candidate = f"{module}.{alias.name}"
                        if module_paths(root, candidate):
                            imports[alias.asname or alias.name] = candidate
                        elif module == "sqlglot":
                            imports[alias.asname or alias.name] = "?"
                        else:
                            imports[alias.asname or alias.name] = module

    used_paths: set[str] = set()
    unresolved = False
    local_names = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func_name = dotted(node.func) or ""
        root_name = func_name.split(".")[0]
        imported_root = imports.get(root_name, root_name)
        source_call = (
            func_name in source_functions
            or (
                func_name.split(".")[-1] in SOURCE_CALLS
                and imported_root in source_modules
            )
        )
        file_read = (
            isinstance(node.func, ast.Name) and node.func.id == "open"
        ) or (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"read_text", "read_bytes"}
        )

        if source_call:
            if not node.args:
                unresolved = True
                continue
            for arg in node.args[:1]:
                ref = dotted(arg)
                if ref:
                    root_name, _, rest = ref.partition(".")
                    imported = imports.get(root_name)
                    if imported == "?":
                        unresolved = True
                    elif imported:
                        module = imported + (("." + rest) if rest else "")
                        paths = resolve_module_paths(root, module.replace(".*", ""))
                        if paths:
                            used_paths.update(paths)
                        else:
                            unresolved = True
                    elif root_name in local_names:
                        continue
                    else:
                        unresolved = True
                else:
                    unresolved = True

        elif file_read:
            if isinstance(node.func, ast.Name):
                first = node.args[0] if node.args else None
            else:
                first = node.func.value

            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                value = first.value.replace("\\\\", "/")
                if value.startswith("sqlglot/") and value.endswith(".py"):
                    if (root / value).is_file():
                        used_paths.add(value)
                    else:
                        unresolved = True
            elif first is not None:
                # Path(module.__file__).read_text() and open(module.__file__)
                # are also direct source reads. Resolve the imported module.
                for candidate in ast.walk(first):
                    if not isinstance(candidate, ast.Attribute) or candidate.attr != "__file__":
                        continue
                    ref = dotted(candidate.value)
                    if not ref:
                        continue
                    root_name, _, rest = ref.partition(".")
                    imported = imports.get(root_name)
                    if imported and imported != "?":
                        module = imported + (("." + rest) if rest else "")
                        used_paths.update(resolve_module_paths(root, module))
                    elif root_name != "__file__":
                        unresolved = True

    return used_paths, unresolved


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    maps_dir = Path(sys.argv[2]).resolve()
    files: dict[str, set[str]] = {
        p.relative_to(root).as_posix(): set()
        for p in (root / "sqlglot").rglob("*.py")
    }
    unknown_test_files: set[str] = set()

    for test_path in (root / "tests").rglob("test_*.py"):
        test_file = test_path.relative_to(root).as_posix()
        paths, unresolved = scan_test(test_path, root)
        for source_path in paths:
            files.setdefault(source_path, set()).add(test_file)
        if unresolved:
            unknown_test_files.add(test_file)

    payload = {
        "files": {path: sorted(test_files) for path, test_files in files.items()},
        "unknown_test_files": sorted(unknown_test_files),
    }
    maps_dir.mkdir(parents=True, exist_ok=True)
    with (maps_dir / "source_readers.pickle").open("wb") as handle:
        pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
    print(
        f"source-reader map: {sum(map(len, files.values()))} file/test links, "
        f"{len(unknown_test_files)} conservative fallback test files",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
