"""Check whether a patch makes sqlglot crash while importing.

A class-body or module-level edit can raise at import time (for example a subclass
that pops a key the edit renamed). Every test in every file that imports the module
then fails.

One checker process imports sqlglot once. For each check it forks a child that serves
the patched files from memory over the pristine tree and imports sqlglot (from scratch
only if a patched module was already loaded), every dialect and the given modules.
Leaf modules such as a dialect file cost a few tens of milliseconds; core modules
cost one full `import sqlglot`.
"""

from __future__ import annotations

import json
import subprocess
import sys

CHECKER = r"""
import importlib, importlib.abc, importlib.util, json, os, sys
root = sys.argv[1]
sys.path.insert(0, root)
import sqlglot  # warm: shared by every forked check
from sqlglot.dialects.dialect import DIALECT_MODULE_NAMES
WARM = json.loads(sys.argv[2])
for _name in WARM + ["sqlglot.dialects." + k for k in DIALECT_MODULE_NAMES]:
    try:
        importlib.import_module(_name)
    except Exception:
        pass

class Loader(importlib.abc.Loader):
    def __init__(self, path, src):
        self.path, self.src = path, src
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        module.__file__ = os.path.join(root, self.path)
        exec(compile(self.src, module.__file__, "exec"), module.__dict__)

def run(request):
    patched = {}
    for p, src in request["files"].items():
        name = p[:-3].replace("/", ".")
        patched[name[: -len(".__init__")] if name.endswith(".__init__") else name] = (p, src)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name not in patched:
                return None
            rel, src = patched[name]
            is_pkg = rel.endswith("__init__.py")
            spec = importlib.util.spec_from_loader(
                name, Loader(rel, src), origin=os.path.join(root, rel), is_package=is_pkg)
            if is_pkg:
                spec.submodule_search_locations = [os.path.dirname(os.path.join(root, rel))]
            return spec

    purge = set(patched) | set(request["dependents"])
    core = request["core"]
    for name in [n for n in sys.modules if n == "sqlglot" or n.startswith("sqlglot.")]:
        if core or name in purge:
            del sys.modules[name]
    sys.meta_path.insert(0, Finder())
    importlib.import_module("sqlglot")
    names = sorted(purge) if not core else WARM + list(patched) + ["sqlglot.dialects." + k for k in DIALECT_MODULE_NAMES]
    for name in names:
        if not name.startswith("tests"):
            importlib.import_module(name)

for line in sys.stdin:
    request = json.loads(line)
    pid = os.fork()
    if pid == 0:
        try:
            run(request)
            code = 0
        except BaseException:
            code = 1
        sys.stdout.flush()
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    print("crash" if os.waitstatus_to_exitcode(status) != 0 else "ok", flush=True)
"""


class ImportChecker:
    """Lazily started checker process; fails safe (any error counts as a crash)."""

    def __init__(self, src_root: str, warm_modules: list[str], dependents: dict[str, list[str]]) -> None:
        self.src_root = src_root
        self.warm = warm_modules
        self.dependents = dependents  # module path -> module names whose import loads it
        self.proc: subprocess.Popen | None = None

    def _start(self) -> subprocess.Popen:
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen(
                [sys.executable, "-c", CHECKER, self.src_root, json.dumps(self.warm)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True,
                env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"},
            )
        return self.proc

    def crashes(self, patched: dict[str, str]) -> bool:
        """Core modules (loaded by `import sqlglot`, so no dependents entry) re-import everything."""
        deps: set[str] = set()
        core = False
        for path in patched:
            if path not in self.dependents:
                core = True
            deps.update(self.dependents.get(path, ()))
        request = {"files": patched, "dependents": sorted(deps), "core": core}
        try:
            proc = self._start()
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
            return proc.stdout.readline().strip() != "ok"
        except (OSError, ValueError):
            self.proc = None
            return True

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
