from __future__ import annotations

from difflib import get_close_matches

from pydantic import BaseModel, Field

from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.permission_helpers import allow_with_mode_policy


class SkillLoadInput(BaseModel):
    name: str
    max_chars: int = Field(default=60000, ge=1, le=60000)


class SkillLoadTool(BaseTool[SkillLoadInput, dict]):
    name = "SkillLoad"
    description = "Load a registered skill's SKILL.md and resource list."
    input_model = SkillLoadInput
    permission_category = "skill"
    state_effect = "external"
    max_result_chars = 60_000

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return ctx.skill_registry is not None

    def is_concurrency_safe(self, input: SkillLoadInput, ctx: ToolExecutionContext) -> bool:
        return True

    def is_read_only(self, input: SkillLoadInput, ctx: ToolExecutionContext) -> bool:
        return True

    async def validate_input(self, input: SkillLoadInput, ctx: ToolExecutionContext) -> ValidationResult:
        if not ctx.skill_registry:
            return ValidationResult(False, "Skill registry is not configured.")
        if not input.name.strip():
            return ValidationResult(False, "name must not be empty.")
        return ValidationResult(True)

    async def check_permissions(self, input: SkillLoadInput, ctx: ToolExecutionContext) -> PermissionDecision:
        return allow_with_mode_policy(self, input, ctx, "Registered skill access allowed.")

    async def call(self, input: SkillLoadInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        if not ctx.skill_registry:
            raise RuntimeError("Skill registry is not configured.")
        skill = ctx.skill_registry.get(input.name)
        if not skill:
            candidates = [item.name for item in ctx.skill_registry.list()]
            matches = get_close_matches(input.name, candidates, n=3)
            hint = f" Did you mean: {', '.join(matches)}?" if matches else ""
            raise RuntimeError(f"Unknown skill: {input.name}.{hint}")
        max_chars = min(input.max_chars, self.max_result_chars)
        text = skill.skill_md.read_text(encoding="utf-8", errors="replace")
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars] + "\n[truncated]"
        return ToolResult({"name": skill.name, "content": text, "resources": skill.resources, "truncated": truncated})
