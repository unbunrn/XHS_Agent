"""Small, deliberately boring tool registry.

The model never executes SQL, shell, or arbitrary Python.  It may only name a
registered tool with JSON arguments; the runner validates the name, the schema
subset and the side-effect/version rules before dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


class ToolError(ValueError):
    pass


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[..., Any]
    side_effect: bool = False
    cost: str = "free"
    idempotent: bool = False
    max_calls: int | None = None


@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    artifact: Any = None
    terminal: str | None = None  # submit / clarify / defer


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if not spec.name or spec.name in self._tools:
            raise ToolError(f"duplicate or empty tool {spec.name!r}")
        self._tools[spec.name] = spec

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> ToolSpec:
        spec = self._tools.get(name)
        if spec is None:
            raise ToolError(f"unknown tool: {name}")
        return spec

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.schema,
                "side_effect": spec.side_effect,
            }
            for spec in sorted(self._tools.values(), key=lambda s: s.name)
        ]

    def validate(self, name: str, args: dict[str, Any]) -> ToolSpec:
        spec = self.get(name)
        if not isinstance(args, dict):
            raise ToolError("args must be an object")
        required = spec.schema.get("required") or []
        for key in required:
            if key not in args:
                raise ToolError(f"{name} missing required argument {key!r}")
        properties = spec.schema.get("properties") or {}
        if properties:
            unknown = sorted(set(args) - set(properties))
            if unknown:
                raise ToolError(f"{name} unknown argument(s): {', '.join(unknown)}")
        return spec

    def execute(self, name: str, args: dict[str, Any], context: Any) -> ToolResult:
        spec = self.validate(name, args)
        try:
            result = spec.handler(args=args, context=context)
        except ToolError:
            raise
        except Exception as exc:
            data: dict[str, Any] = {}
            issues = getattr(exc, "issues", None)
            if issues:
                data["issues"] = issues
            candidate = getattr(exc, "candidate", None)
            if candidate:
                # The runner persists this so the model can make one targeted
                # repair instead of guessing from a single English string.
                data["candidate"] = candidate
            return ToolResult(
                ok=False,
                error=f"{type(exc).__name__}: {exc}"[:500],
                data=data,
            )
        if isinstance(result, ToolResult):
            return result
        if isinstance(result, dict):
            return ToolResult(ok=True, data=result)
        return ToolResult(ok=True, data={"result": result})
