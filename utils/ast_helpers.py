from __future__ import annotations
import ast
def collect_string_values(syntax_node) -> list[str]:
    """Recursively gather string constants from an AST node."""
    result_value: list[str] = []
    for child_node in ast.walk(syntax_node):
        if isinstance(child_node, ast.Constant) and isinstance(child_node.value, str):
            result_value.append(child_node.value)
    return result_value
def resolve_decorator_target_name(decorator_node) -> str | None:
    """Read the dotted target name from an AST decorator node."""
    decorator_target = decorator_node.func if isinstance(decorator_node, ast.Call) else decorator_node
    url_parts: list[str] = []
    while isinstance(decorator_target, ast.Attribute):
        url_parts.append(decorator_target.attr)
        decorator_target = decorator_target.value
    if not isinstance(decorator_target, ast.Name):
        return None
    url_parts.append(decorator_target.id)
    return ".".join(reversed(url_parts))
def matches_pytest_mark_decorator(decorator_node, mark_name) -> bool:
    """Return True for @pytest.mark.<mark_name> and its call-based form."""
    decorator_target_name = resolve_decorator_target_name(decorator_node)
    return decorator_target_name == f"pytest.mark.{mark_name}"
