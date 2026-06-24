"""权限决策管道：decide_permission 及其依赖的构建/检查函数。"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from ..base import BaseTool, PermissionDecision, ToolExecutionContext
from .models import PermissionTarget
from .modes import _apply_generic_defaults
from .rules import RuleEngine, _decision_from_rule, _is_unrelaxable
from .safety import check_safety_for_target


# ---------------------------------------------------------------------------
# Engine factory
# ---------------------------------------------------------------------------

def _engine_from_context(ctx: ToolExecutionContext) -> RuleEngine:
    return RuleEngine(
        allow_rules=ctx.permission_context.always_allow,
        deny_rules=ctx.permission_context.always_deny,
        ask_rules=ctx.permission_context.always_ask,
    )


# ---------------------------------------------------------------------------
# Main decision pipeline
# ---------------------------------------------------------------------------

async def decide_permission(
    tool: BaseTool,
    input_model: BaseModel,
    ctx: ToolExecutionContext,
) -> PermissionDecision:
    """按整工具规则、工具内容检查、bypass、整工具 allow 的顺序收敛权限。"""

    target = build_permission_target(tool, input_model)
    engine = _engine_from_context(ctx)

    # 1. 整工具 deny。只看工具名，不看参数。
    whole_rule = engine.match_tool(target, "deny")
    if whole_rule:
        return _decision_from_rule(whole_rule, "deny", "Denied by explicit tool rule.")

    # 2. 整工具 ask。命中后不执行工具内部检查，也不允许 bypass/allow 放宽。
    whole_rule = engine.match_tool(target, "ask")
    if whole_rule:
        return _decision_from_rule(whole_rule, "ask", "Permission required by explicit tool rule.")

    # 3. 工具检查具体输入。工具负责内容级 deny/safety/ask/allow。
    tool_decision = await tool.check_permissions(input_model, ctx)
    if tool_decision.updated_input is None:
        tool_decision.updated_input = input_model

    if tool_decision.behavior == "passthrough":
        decision = _apply_generic_defaults(tool_decision, tool, target, ctx)
    else:
        decision = tool_decision

    # 4. 不可放宽结果直接返回。
    if _is_unrelaxable(decision):
        return decision

    # 5. bypassPermissions 只放宽普通 ask/passthrough。
    if ctx.permission_context.mode == "bypassPermissions":
        return PermissionDecision(
            "allow", message="Allowed by bypassPermissions mode.",
            updated_input=input_model, decision_reason={"type": "mode"},
        )

    # 6. 整工具 allow 只能放宽剩余普通结果。
    whole_rule = engine.match_tool(target, "allow")
    if whole_rule:
        return _decision_from_rule(whole_rule, "allow", "Allowed by explicit tool rule.")

    # 7. 使用工具/通用层剩余结果；passthrough 没有自动允许依据，转 ask。
    if decision.behavior == "allow":
        return decision
    if decision.behavior == "ask":
        return decision
    if decision.behavior == "passthrough":
        return PermissionDecision(
            "ask",
            message=f"{tool.name} requires permission.",
            updated_input=input_model,
            decision_reason={"type": "ordinary"},
        )
    return decision


# ---------------------------------------------------------------------------
# Permission target builder
# ---------------------------------------------------------------------------

def build_permission_target(tool: BaseTool, input_model: BaseModel) -> PermissionTarget:
    """从 Pydantic 输入模型中抽取 path/command/url 等权限判断字段。"""

    data = input_model.model_dump()
    path_value = data.get("file_path") or data.get("path")
    return PermissionTarget(
        tool_name=tool.name,
        category=tool.permission_category,
        path=Path(path_value) if path_value else None,
        command=data.get("command") or data.get("subagent_type"),
        network_url=data.get("url"),
        raw=input_model,
    )


# ---------------------------------------------------------------------------
# Content-level policy (used by individual tools)
# ---------------------------------------------------------------------------

def check_content_policy(
    target: PermissionTarget, ctx: ToolExecutionContext
) -> PermissionDecision | None:
    """工具内部可调用的标准内容级权限顺序。"""

    engine = _engine_from_context(ctx)

    rule = engine.match_content(target, "deny")
    if rule:
        return _decision_from_rule(rule, "deny", "Denied by explicit content rule.")

    safety = check_safety_for_target(target, ctx)
    if safety:
        return safety

    rule = engine.match_content(target, "ask")
    if rule:
        return _decision_from_rule(rule, "ask", "Permission required by explicit content rule.")

    rule = engine.match_content(target, "allow")
    if rule:
        return _decision_from_rule(rule, "allow", "Allowed by explicit content rule.")

    return None


# ---------------------------------------------------------------------------
# Mode policy (used by individual tools)
# ---------------------------------------------------------------------------

def check_mode_policy_for_target(
    target: PermissionTarget,
    ctx: ToolExecutionContext,
    tool: BaseTool | None = None,
) -> PermissionDecision | None:
    """工具返回精确 allow 前可调用的模式收紧检查。"""

    if ctx.permission_context.mode == "plan":
        from .modes import _is_plan_file_write
        if _is_plan_file_write(target, ctx):
            return PermissionDecision(
                "allow",
                message="Plan file write allowed in Plan Mode.",
                updated_input=target.raw,
                decision_reason={"type": "mode"},
            )
        if target.category == "state":
            try:
                is_read_only = bool(tool.is_read_only(target.raw, ctx)) if tool is not None and target.raw is not None else False
            except Exception:
                is_read_only = False
            if not is_read_only:
                return PermissionDecision(
                    "deny",
                    message=f"{target.tool_name} is not allowed in Plan Mode.",
                    updated_input=target.raw,
                    decision_reason={"type": "mode"},
                )
    return None
