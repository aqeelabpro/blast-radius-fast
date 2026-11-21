"""
FAST Team - AST utilities for code analysis
"""
from __future__ import annotations
import ast
from typing import List

FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def start(node: ast.AST) -> int:
    """Get first line of node, including decorators."""
    decorators = getattr(node, "decorator_list", None) or []
    lines = [node.lineno] + [d.lineno for d in decorators]
    return min(lines)


def chain(tree: ast.AST, line: int) -> List[ast.AST]:
    """Get nodes (outermost first) whose range contains the line."""
    result: List[ast.AST] = []
    current = tree

    while True:
        found = None
        for child in ast.iter_child_nodes(current):
            if not hasattr(child, "lineno"):
                continue
            end = getattr(child, "end_lineno", None)
            if end is None:
                continue

            if start(child) <= line <= end:
                found = child
                # Prefer stmt/Lambda over other nodes
                if isinstance(child, (ast.stmt, ast.Lambda)):
                    break

        if found is None:
            return result

        result.append(found)
        current = found


def own_lines(stmt: ast.stmt) -> tuple:
    """
    Get lines the statement itself executes (not its body).
    For compound statements, returns the header lines.
    """
    # Find first line of any body/handler/etc
    body_attrs = ("body", "handlers", "orelse", "finalbody", "cases")
    child_lines = []
    for attr in body_attrs:
        children = getattr(stmt, attr, None) or []
        for child in children:
            if hasattr(child, "lineno"):
                child_lines.append(child.lineno)

    stmt_start = start(stmt)
    if not child_lines:
        return stmt_start, stmt.end_lineno

    # Header ends before first child
    return stmt_start, max(stmt_start, min(child_lines) - 1)
