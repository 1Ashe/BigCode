from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.permission_helpers import allow_with_mode_policy


class QuestionOption(BaseModel):
    label: str
    description: str = ""


class UserQuestion(BaseModel):
    question: str
    kind: Literal["single", "multiple"] = "single"
    options: list[QuestionOption] = Field(min_length=1, description="Options with the recommended option first.")

    @field_validator("options")
    @classmethod
    def _validate_options(cls, value: list[QuestionOption]) -> list[QuestionOption]:
        if not value or not value[0].label.strip():
            raise ValueError("The first option must be the recommended option.")
        if any(not option.label.strip() for option in value):
            raise ValueError("options must not contain blank labels.")
        return value


class AskUserQuestionInput(BaseModel):
    questions: list[UserQuestion] = Field(
        min_length=1,
        max_length=3,
        description="Ask 1-3 questions. Each question may be single or multiple choice.",
    )


class AskUserQuestionTool(BaseTool[AskUserQuestionInput, dict]):
    name = "AskUserQuestion"
    description = (
        "Ask the user one to three clarification questions when a material decision cannot be resolved from "
        "repo inspection. Use concise questions with meaningful options and put the recommended option first. "
        "In non-interactive sessions, this returns requires_answer instead of blocking."
    )
    input_model = AskUserQuestionInput
    permission_category = "state"
    state_effect = "app_state"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return True

    def is_concurrency_safe(self, input: AskUserQuestionInput, ctx: ToolExecutionContext) -> bool:
        return False

    def is_read_only(self, input: AskUserQuestionInput, ctx: ToolExecutionContext) -> bool:
        return False

    async def validate_input(self, input: AskUserQuestionInput, ctx: ToolExecutionContext) -> ValidationResult:
        if not 1 <= len(input.questions) <= 3:
            return ValidationResult(False, "AskUserQuestion requires 1-3 questions.")
        return ValidationResult(True)

    async def check_permissions(self, input: AskUserQuestionInput, ctx: ToolExecutionContext) -> PermissionDecision:
        return allow_with_mode_policy(self, input, ctx, "User question allowed.")

    async def call(self, input: AskUserQuestionInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        if ctx.is_non_interactive_session:
            return ToolResult({"requires_answer": True, "questions": [question.model_dump() for question in input.questions]})
        if ctx.terminal_interaction_callback is not None:
            answers = await ctx.terminal_interaction_callback(lambda: _ask_questions(input.questions))
        else:
            answers = await asyncio.to_thread(_ask_questions, input.questions)
        return ToolResult({"answers": answers})


def _ask_questions(questions: list[UserQuestion]) -> list[dict]:
    answers: list[dict] = []
    for q_idx, question in enumerate(questions, start=1):
        print(f"\nQuestion {q_idx}: {question.question}")
        for idx, option in enumerate(question.options, start=1):
            suffix = " [recommended]" if idx == 1 else ""
            description = f" - {option.description}" if option.description else ""
            print(f"  {idx}. {option.label}{suffix}{description}")
        other_index = len(question.options) + 1
        print(f"  {other_index}. Other")
        raw = input("Answer: ").strip()
        if question.kind == "multiple":
            answers.append({"question": question.question, "kind": question.kind, "answers": _parse_multiple_answers(raw, question, other_index)})
        else:
            answers.append({"question": question.question, "kind": question.kind, "answer": _parse_single_answer(raw, question, other_index)})
    return answers


def _parse_single_answer(raw: str, question: UserQuestion, other_index: int) -> str:
    if not raw:
        return question.options[0].label
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(question.options):
            return question.options[idx - 1].label
        if idx == other_index:
            return input("Other: ").strip()
    return raw


def _parse_multiple_answers(raw: str, question: UserQuestion, other_index: int) -> list[str]:
    if not raw:
        return [question.options[0].label]
    selected: list[str] = []
    for token in [part.strip() for part in raw.split(",") if part.strip()]:
        if token.isdigit():
            idx = int(token)
            if 1 <= idx <= len(question.options):
                selected.append(question.options[idx - 1].label)
            elif idx == other_index:
                other = input("Other: ").strip()
                if other:
                    selected.append(other)
        else:
            selected.append(token)
    return selected or [question.options[0].label]
