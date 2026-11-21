"""
FAST Team - Import crash detection
Checks if patched modules would crash on import.
"""
from __future__ import annotations
from typing import Dict, List


class ImportChecker:
    """
    Detects if patched code would crash during import.

    When a patch has a syntax error or import-time exception,
    we need to run all tests to catch the failure properly.
    """

    def __init__(self, src_root: str, modules: List[str], dependents: Dict):
        self.src_root = src_root
        self.modules = modules
        self.dependents = dependents

    def crashes(self, patches: Dict[str, str]) -> bool:
        """
        Check if patched modules would crash on import.

        Uses compile() as a quick syntax check - catches most issues.
        """
        if not patches:
            return False

        for path, content in patches.items():
            try:
                compile(content, path, "exec")
            except SyntaxError:
                return True

        return False

    def close(self):
        """Cleanup (no-op in this simple implementation)."""
        pass
