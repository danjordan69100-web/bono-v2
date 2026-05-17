"""Bono v2 — Tool registry pour Anthropic native tool calling.

Décorateur @bono_tool qui auto-enregistre dans TOOLS_REGISTRY avec schemas Anthropic-compatible.
Sépare DEFINITION (schema JSON envoyé à Anthropic) et IMPLEMENTATION (callable Python).
"""
from typing import Callable, Any
from functools import wraps

TOOLS_REGISTRY: dict[str, dict] = {}


def bono_tool(name: str, description: str, parameters: dict):
    """Decorator. parameters = JSON Schema object."""
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        schema = {
            "name": name,
            "description": description,
            "input_schema": parameters,
        }
        TOOLS_REGISTRY[name] = {"schema": schema, "func": func}
        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper
    return decorator


def get_all_schemas() -> list[dict]:
    """Returns list of tool schemas to pass to Anthropic API tools= param."""
    return [t["schema"] for t in TOOLS_REGISTRY.values()]


def dispatch(name: str, tool_input: dict) -> dict:
    """Call the registered tool by name. Returns dict (will be json.dumps'd for Anthropic tool_result)."""
    entry = TOOLS_REGISTRY.get(name)
    if not entry:
        return {"error": "tool_not_found", "tool": name}
    try:
        return entry["func"](**(tool_input or {}))
    except Exception as e:
        return {"error": str(e), "tool": name}
