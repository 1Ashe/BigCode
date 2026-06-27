from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bigcode.commands import Command, CommandRegistry, CommandType, complete, parse_command
from bigcode.commands.handlers import register_all_commands
from bigcode.config.models import CompactConfig
from bigcode.tools.permissions import ToolPermissionContext
from bigcode.ui.repl import BigCodeRepl


class FakeUI:
    def __init__(self) -> None:
        self.prints: list[tuple[tuple[object, ...], str]] = []
        self.tables: list[dict[str, object]] = []

    def print(self, *objects: object, end: str = "\n") -> None:
        self.prints.append((objects, end))

    def status_table(self, rows: dict[str, object]) -> None:
        self.tables.append(rows)

    def text(self) -> str:
        return "\n".join(str(obj) for objects, _ in self.prints for obj in objects)


class FakeSkillRegistry:
    def __init__(self) -> None:
        self.skills = [
            SimpleNamespace(
                name="review",
                description="Review code",
                source="skill",
                skill_md=Path("/tmp/review/SKILL.md"),
                resources=["notes.md"],
                plugin_name=None,
            )
        ]

    def list(self) -> list[object]:
        return self.skills

    def get(self, name: str) -> object | None:
        return next((skill for skill in self.skills if skill.name == name), None)

    def status_counts(self) -> dict[str, int]:
        return {"enabled": len(self.skills), "disabled": 0, "failed": 0}


class FakeMcpManager:
    enabled = True
    fastmcp_available = False
    capabilities: list[object] = []

    def __init__(self) -> None:
        self.servers = {
            "docs": SimpleNamespace(enabled=True, description="Docs server"),
        }
        self.discovered = False

    async def discover(self) -> list[object]:
        self.discovered = True
        self.capabilities = [object()]
        return self.capabilities


class FakeSession:
    def __init__(self, root: Path) -> None:
        self.session_id = "sess_1"
        self.model_ref = "model"
        self.config = SimpleNamespace(cwd=root, project_state_dir=root / ".bigcode", compact=CompactConfig())
        self.messages: list[object] = []
        self.loaded_skills: set[str] = set()
        self.last_verification = None
        self.agent_task_store = SimpleNamespace(status_counts=lambda: {"queued": 0, "running": 0, "completed": 0, "failed": 0, "cancelled": 0})
        self.registry = SimpleNamespace()
        self.mcp_manager = FakeMcpManager()
        self.skill_registry = FakeSkillRegistry()
        self.permission_context = ToolPermissionContext(mode="default")
        self.persist_snapshot = True
        self.transcript = SimpleNamespace(path=root / ".bigcode" / "transcripts" / "sess_1.jsonl")
        self.plan_state = SimpleNamespace(active=False)
        self.memory_manager = SimpleNamespace(load_index_for_prompt=lambda: "## User Memories\n- prefers tests")
        self.saved = False

    def model_protocol_label(self) -> str:
        return "openai"

    def _save_snapshot(self) -> None:
        self.saved = True


class SlashCommandTests(unittest.TestCase):
    def test_parse_command(self) -> None:
        self.assertEqual(parse_command("hello"), ("", "", False))
        self.assertEqual(parse_command("/"), ("", "", True))
        self.assertEqual(parse_command("/HELP status"), ("help", "status", True))

    def test_registry_rejects_alias_conflicts_and_completes_aliases(self) -> None:
        async def noop(ctx):
            return False

        registry = CommandRegistry()
        registry.register_sync(Command("status", "Show status", CommandType.LOCAL, noop, aliases=["s"]))
        with self.assertRaises(ValueError):
            registry.register_sync(Command("s", "Conflict", CommandType.LOCAL, noop))

        matches = complete(registry, "/s")
        self.assertEqual(matches[0][1], "/status")
        self.assertIn("Show status", matches[0][0])

    def test_help_and_exit_alias(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ui = FakeUI()
            repl = BigCodeRepl(FakeSession(Path(td)), ui=ui)  # type: ignore[arg-type]
            should_exit = asyncio.run(repl.handle_command("/q"))
            self.assertTrue(should_exit)
            self.assertFalse(asyncio.run(repl.handle_command("/help status")))

        self.assertIn("/status", ui.text())
        self.assertIn("Show current session status", ui.text())

    def test_status_skill_mcp_permission_and_memory_commands(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            session = FakeSession(Path(td))
            ui = FakeUI()
            repl = BigCodeRepl(session, ui=ui)  # type: ignore[arg-type]

            self.assertFalse(asyncio.run(repl.handle_command("/status")))
            self.assertFalse(asyncio.run(repl.handle_command("/skill info review")))
            self.assertFalse(asyncio.run(repl.handle_command("/mcp discover")))
            self.assertFalse(asyncio.run(repl.handle_command("/permission acceptEdits")))
            self.assertFalse(asyncio.run(repl.handle_command("/memory")))

        self.assertEqual(ui.tables[0]["session"], "sess_1")
        self.assertEqual(ui.tables[1]["name"], "review")
        self.assertEqual(ui.tables[2]["discovered capabilities"], 1)
        self.assertEqual(session.permission_context.mode, "acceptEdits")
        self.assertTrue(session.saved)
        self.assertIn("prefers tests", ui.text())

    def test_doctor_args_are_preserved(self) -> None:
        async def fake_report(config, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(overall_status="OK", items=[], active_model_ref="model", probe_enabled=False)

        calls: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as td:
            ui = FakeUI()
            repl = BigCodeRepl(FakeSession(Path(td)), ui=ui)  # type: ignore[arg-type]
            with patch("bigcode.commands.handlers.core.build_doctor_report", fake_report), patch(
                "bigcode.commands.handlers.core.render_doctor_report", lambda report: "doctor ok\n"
            ):
                self.assertFalse(asyncio.run(repl.handle_command("/doctor --no-probe --timeout 1.5")))

        self.assertFalse(calls[0]["probe"])
        self.assertEqual(calls[0]["timeout"], 1.5)
        self.assertIn("doctor ok", ui.text())

    def test_all_commands_register_without_conflicts(self) -> None:
        registry = CommandRegistry()
        register_all_commands(registry)
        names = [command.name for command in registry.list_commands()]
        self.assertIn("help", names)
        self.assertIn("permission", names)
        self.assertIsNotNone(registry.find("quit"))


if __name__ == "__main__":
    unittest.main()
