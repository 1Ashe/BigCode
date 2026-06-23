from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.permission_helpers import allow_with_mode_policy
from bigcode.tools.permissions import SENSITIVE_NAMES


class SkillResourceReadInput(BaseModel):
    name: str
    resource_path: str


class SkillResourceReadTool(BaseTool[SkillResourceReadInput, dict]):
    name = "SkillResourceRead"
    description = (
        "Read a resource file from a registered skill. Use this only after SkillLoad or skill instructions point "
        "to a specific resource. Provide the skill name and resource path; the tool enforces skill resource "
        "boundaries but may expose external instructional content."
    )
    input_model = SkillResourceReadInput
    permission_category = "skill"
    state_effect = "external"
    max_result_chars = 80_000

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return ctx.skill_registry is not None

    def is_concurrency_safe(self, input: SkillResourceReadInput, ctx: ToolExecutionContext) -> bool:
        return True

    def is_read_only(self, input: SkillResourceReadInput, ctx: ToolExecutionContext) -> bool:
        return True

    async def validate_input(self, input: SkillResourceReadInput, ctx: ToolExecutionContext) -> ValidationResult:
        if not ctx.skill_registry:
            return ValidationResult(False, "Skill registry is not configured.")
        if not input.name.strip():
            return ValidationResult(False, "name must not be empty.")
        rel = Path(input.resource_path)
        if rel.is_absolute() or ".." in rel.parts or rel.name in SENSITIVE_NAMES:
            return ValidationResult(False, "Invalid skill resource path.")
        return ValidationResult(True)

    async def check_permissions(self, input: SkillResourceReadInput, ctx: ToolExecutionContext) -> PermissionDecision:
        return allow_with_mode_policy(self, input, ctx, "Registered skill resource access allowed.")

    async def call(self, input: SkillResourceReadInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        if not ctx.skill_registry:
            raise RuntimeError("Skill registry is not configured.")
        skill = ctx.skill_registry.get(input.name)
        if not skill:
            raise RuntimeError(f"Unknown skill: {input.name}")
        rel = Path(input.resource_path)
        if rel.is_absolute() or ".." in rel.parts or rel.name in SENSITIVE_NAMES:
            raise RuntimeError("Invalid skill resource path.")
        try:
            path = (skill.root / rel).resolve(strict=True)
            path.relative_to(skill.root)
        except Exception as exc:
            raise RuntimeError("Invalid skill resource path.") from exc
        if not path.is_file():
            raise RuntimeError("Skill resource is not a file.")
        return ToolResult({"name": skill.name, "resource_path": str(rel), "content": path.read_text(encoding="utf-8", errors="replace")})
