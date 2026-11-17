"""Runtime context passed to tool handlers."""

from __future__ import annotations

from typing import Any

from .contracts import ArtifactVersion, CreativeBrief, Material
from .store import AgentStore


class AgentContext:
    def __init__(self, *, brief: CreativeBrief, runtime: Any, store: AgentStore,
                 base_artifact: ArtifactVersion | None = None):
        self.brief = brief
        self.runtime = runtime
        self.store = store
        self.base_artifact = base_artifact
        self._materials: dict[str, Material] = {}

    def add_material(self, material: Material) -> Material:
        self._materials[material.material_id] = material
        return material

    def material(self, material_id: str) -> Material | None:
        return self._materials.get(material_id)

    @property
    def materials(self) -> list[Material]:
        return list(self._materials.values())

    def latest_artifact(self) -> ArtifactVersion | None:
        return self.store.latest_artifact(self.brief.run_key) or self.base_artifact
