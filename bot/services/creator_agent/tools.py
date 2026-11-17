"""Registered tools for Creator Agent V2.

The handlers are thin.  A runtime adapter supplies the actual search, writing,
rendering and approval operations, so the loop can be tested offline without
calling a real model, Feishu, Xiaohongshu, or the network.
"""

from __future__ import annotations

from typing import Any

from .contracts import ArtifactVersion, ClaimAssessment, CreativeBrief, Material, Review
from .registry import ToolResult, ToolSpec

TOOL_NAMES = [
    "discover_topics",
    "search",
    "read_source",
    "inspect_claims",
    "recall_context",
    "draft_artifact",
    "revise_artifact",
    "render_images",
    "inspect_layout",
    "review_artifact",
    "submit_for_approval",
    "ask_owner",
]


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


def _runtime(context: Any) -> Any:
    runtime = getattr(context, "runtime", None)
    if runtime is None:
        raise RuntimeError("agent context has no runtime")
    return runtime


def register_tools(registry, runtime: Any) -> None:
    """Register the full V2 tool surface against one runtime adapter."""

    def discover(args, context):
        result = _runtime(context).discover_topics(limit=int(args.get("limit", 10)))
        return ToolResult(ok=True, data={"topics": list(result)})

    def search(args, context):
        materials = _runtime(context).search(
            str(args["query"]),
            limit=int(args.get("limit", 8)),
            mode=str(args.get("mode", "news")),
        )
        for material in materials:
            context.add_material(material)
        runtime = _runtime(context)
        return ToolResult(ok=True, data={
            "materials": [m.as_dict() for m in materials],
            "physical_fetches": int(getattr(runtime, "last_physical_fetches", 0)),
        })

    def read_source(args, context):
        material = _runtime(context).read_source(
            material_id=args.get("material_id"),
            url=args.get("url"),
        )
        context.add_material(material)
        runtime = _runtime(context)
        return ToolResult(ok=True, data={
            "material": material.as_dict(),
            "physical_fetches": int(getattr(runtime, "last_physical_fetches", 0)),
        })

    def inspect_claims(args, context):
        claims = _runtime(context).inspect_claims(
            claims=list(args.get("claims") or []),
            material_ids=list(args.get("material_ids") or []),
        )
        return ToolResult(ok=True, data={"claims": [c.as_dict() for c in claims]})

    def recall(args, context):
        return ToolResult(
            ok=True,
            data={"preferences": _runtime(context).recall_context(str(args.get("scope", "")))},
        )

    def draft(args, context):
        artifact = _runtime(context).draft_artifact(
            brief=context.brief,
            instruction=str(args.get("instruction", "")),
            material_ids=list(args.get("material_ids") or []),
            base_version=args.get("base_version"),
        )
        return ToolResult(ok=True, data={"artifact": artifact.as_dict()}, artifact=artifact)

    def revise(args, context):
        artifact = _runtime(context).revise_artifact(
            brief=context.brief,
            base_artifact=context.latest_artifact(),
            instruction=str(args.get("instruction", "")),
            revision_plan=args.get("revision_plan") or {},
            material_ids=list(args.get("material_ids") or []),
        )
        return ToolResult(ok=True, data={"artifact": artifact.as_dict()}, artifact=artifact)

    def render(args, context):
        artifact = _runtime(context).render_images(
            brief=context.brief,
            artifact=context.latest_artifact(),
            layouts=list(args.get("layouts") or []),
        )
        return ToolResult(ok=True, data={"artifact": artifact.as_dict()}, artifact=artifact)

    def inspect(args, context):
        issues = _runtime(context).inspect_layout(context.latest_artifact())
        return ToolResult(ok=True, data={"issues": list(issues)})

    def review(args, context):
        review_obj = _runtime(context).review_artifact(
            brief=context.brief,
            artifact=context.latest_artifact(),
            materials=context.materials,
        )
        return ToolResult(ok=True, data={"review": review_obj.as_dict()})

    def submit(args, context):
        result = _runtime(context).submit_for_approval(
            brief=context.brief,
            artifact=context.latest_artifact(),
        )
        if isinstance(result, ToolResult):
            return result
        return ToolResult(ok=True, data=dict(result or {}), terminal="submit")

    def ask(args, context):
        question = str(args.get("question", "")).strip() or "请确认下一步"
        _runtime(context).ask_owner(question)
        return ToolResult(ok=True, data={"question": question}, terminal="clarify")

    specs = [
        ToolSpec(
            "discover_topics",
            "List scheduled/discoverable topics. Read-only.",
            _obj({"limit": {"type": "integer"}}),
            discover,
            cost="free",
            idempotent=True,
        ),
        ToolSpec(
            "search",
            "Search for fresh materials; returns material IDs only. Read-only.",
            _obj({"query": {"type": "string"}, "limit": {"type": "integer"}, "mode": {"type": "string"}}, ["query"]),
            search,
            cost="search",
            idempotent=True,
            max_calls=8,
        ),
        ToolSpec(
            "read_source",
            "Fetch one source body by material_id or URL. Read-only.",
            _obj({"material_id": {"type": "string"}, "url": {"type": "string"}}),
            read_source,
            cost="fetch",
            idempotent=True,
            max_calls=6,
        ),
        ToolSpec(
            "inspect_claims",
            "Assess candidate claims against supplied material IDs.",
            _obj({"claims": {"type": "array"}, "material_ids": {"type": "array"}}),
            inspect_claims,
            cost="free",
            idempotent=True,
        ),
        ToolSpec(
            "recall_context",
            "Read scoped owner preferences. Read-only.",
            _obj({"scope": {"type": "string"}}),
            recall,
            cost="free",
            idempotent=True,
        ),
        ToolSpec(
            "draft_artifact",
            "Create an internal draft. Does not create an approval.",
            _obj({"instruction": {"type": "string"}, "material_ids": {"type": "array"}, "base_version": {"type": "integer"}}),
            draft,
            side_effect=True,
            cost="model",
            idempotent=False,
        ),
        ToolSpec(
            "revise_artifact",
            "Create a new artifact version from a revision plan.",
            _obj({"instruction": {"type": "string"}, "revision_plan": {"type": "object"}, "material_ids": {"type": "array"}}),
            revise,
            side_effect=True,
            cost="model",
            idempotent=False,
        ),
        ToolSpec(
            "render_images",
            "Render image cards from the latest artifact.",
            _obj({"layouts": {"type": "array"}}),
            render,
            side_effect=True,
            cost="local",
            idempotent=False,
        ),
        ToolSpec(
            "inspect_layout",
            "Deterministically inspect rendered images for layout problems.",
            _obj({}),
            inspect,
            cost="free",
            idempotent=True,
        ),
        ToolSpec(
            "review_artifact",
            "Run an independent evidence/style review.",
            _obj({}),
            review,
            cost="model",
            idempotent=True,
        ),
        ToolSpec(
            "submit_for_approval",
            "Submit the latest complete artifact for owner approval. Terminal.",
            _obj({}),
            submit,
            side_effect=True,
            idempotent=True,
        ),
        ToolSpec(
            "ask_owner",
            "Ask the owner for information and stop. Terminal.",
            _obj({"question": {"type": "string"}}, ["question"]),
            ask,
            side_effect=True,
            idempotent=True,
        ),
    ]
    for spec in specs:
        registry.register(spec)
