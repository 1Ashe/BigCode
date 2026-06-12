from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from bigcode.config import load_runtime_config
from bigcode.diagnostics import build_doctor_report, render_doctor_report
from bigcode.hooks.command import CommandRegistry, command_hooks_from_settings
from bigcode.skills.loader import load_skills


def _write_model_config(root: Path) -> None:
    cfg = root / ".bigcode"
    cfg.mkdir(exist_ok=True)
    (cfg / "models.json").write_text(
        """
        {
          "default_model": "local:test",
          "providers": {
            "local": {
              "base_url": "https://api.example.test/v1",
              "models": {"test": {"id": "test-model"}}
            }
          }
        }
        """,
        encoding="utf-8",
    )


class Phase3EcosystemTests(unittest.TestCase):
    def test_manifest_loads_multiple_skills(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plugin = root / "plugins" / "demo-plugin"
            (plugin / ".codex-plugin").mkdir(parents=True)
            (plugin / "skills" / "alpha").mkdir(parents=True)
            (plugin / "skills" / "beta").mkdir(parents=True)
            (plugin / ".codex-plugin" / "plugin.json").write_text(
                """
                {
                  "name": "demo-plugin",
                  "description": "Demo plugin",
                  "version": "0.1.0",
                  "skills": [
                    {"name": "alpha-skill", "path": "skills/alpha/SKILL.md"},
                    {"name": "beta-skill", "path": "skills/beta/SKILL.md", "description": "Beta override"}
                  ]
                }
                """,
                encoding="utf-8",
            )
            (plugin / "skills" / "alpha" / "SKILL.md").write_text("---\nname: ignored-alpha\n---\nAlpha.", encoding="utf-8")
            (plugin / "skills" / "beta" / "SKILL.md").write_text("---\nname: ignored-beta\n---\nBeta.", encoding="utf-8")

            registry = load_skills([root / "plugins"], include_builtin=False)
            self.assertEqual({skill.name for skill in registry.list()}, {"alpha-skill", "beta-skill"})
            self.assertEqual(registry.get("beta-skill").description, "Beta override")
            self.assertEqual(registry.status_counts(), {"enabled": 2, "disabled": 0, "failed": 0})

    def test_manifest_disabled_and_failed_entries_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plugin = root / "plugins" / "demo-plugin"
            (plugin / ".codex-plugin").mkdir(parents=True)
            (plugin / "enabled").mkdir(parents=True)
            (plugin / "enabled" / "SKILL.md").write_text("---\nname: enabled-skill\n---\nEnabled.", encoding="utf-8")
            (plugin / ".codex-plugin" / "plugin.json").write_text(
                """
                {
                  "name": "demo-plugin",
                  "skills": [
                    {"name": "enabled-skill", "path": "enabled/SKILL.md"},
                    {"name": "off-skill", "path": "enabled/SKILL.md", "enabled": false},
                    {"name": "bad-path", "path": "../escape/SKILL.md"},
                    {"name": "BadName", "path": "enabled/SKILL.md"}
                  ]
                }
                """,
                encoding="utf-8",
            )

            registry = load_skills([root / "plugins"], include_builtin=False)
            self.assertEqual([skill.name for skill in registry.list()], ["enabled-skill"])
            counts = registry.status_counts()
            self.assertEqual(counts["enabled"], 1)
            self.assertEqual(counts["disabled"], 1)
            self.assertEqual(counts["failed"], 2)
            self.assertTrue(any("relative" in report.reason for report in registry.reports if report.status == "failed"))
            self.assertTrue(any("invalid skill name" in report.reason for report in registry.reports if report.status == "failed"))

    def test_builtin_skills_are_default_and_can_be_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = load_skills([], include_builtin=True)
            names = {skill.name for skill in registry.list()}
            self.assertTrue({"repo-map", "code-review", "test-debug"}.issubset(names))

            override = root / "skills" / "repo-map"
            override.mkdir(parents=True)
            (override / "SKILL.md").write_text("---\nname: repo-map\ndescription: Override skill\n---\nProject version.", encoding="utf-8")
            overridden = load_skills([root / "skills"], include_builtin=True)
            self.assertEqual(overridden.get("repo-map").description, "Override skill")
            self.assertEqual(overridden.get("repo-map").source, "skill")

    def test_command_registry_validates_without_throwing(self) -> None:
        settings = {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {"type": "command", "command": "python check.py", "timeout": 5},
                        {"type": "command", "timeout": 5},
                        {"type": "command", "command": "python disabled.py", "enabled": False},
                    ],
                }
            ],
            "NotAnEvent": [{"hooks": [{"type": "command", "command": "python bad.py"}]}],
        }
        registry = CommandRegistry.from_settings(settings)
        self.assertEqual(registry.status_counts(), {"enabled": 1, "disabled": 1, "failed": 2})
        handlers = registry.enabled_handlers()
        self.assertEqual(len(handlers), 1)
        self.assertEqual(handlers[0].spec.command, "python check.py")
        self.assertEqual(command_hooks_from_settings(settings)[0].spec.matcher, "Bash")

    def test_plugin_commands_are_reported_as_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plugin = root / "plugins" / "demo-plugin"
            (plugin / ".codex-plugin").mkdir(parents=True)
            (plugin / ".codex-plugin" / "plugin.json").write_text(
                """
                {
                  "name": "demo-plugin",
                  "commands": [{"event": "Stop", "command": "python plugin.py"}]
                }
                """,
                encoding="utf-8",
            )
            registry = CommandRegistry.from_settings({}, plugin_roots=[root / "plugins"])
            self.assertEqual(registry.status_counts(), {"enabled": 0, "disabled": 1, "failed": 0})
            self.assertIn("not supported", registry.registrations[0].reason)

    def test_doctor_reports_skill_and_command_status(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_model_config(root)
            plugin = root / "plugins" / "demo-plugin"
            (plugin / ".codex-plugin").mkdir(parents=True)
            (plugin / "skill").mkdir(parents=True)
            (plugin / "skill" / "SKILL.md").write_text("---\nname: plugin-skill\n---\nPlugin skill.", encoding="utf-8")
            (plugin / ".codex-plugin" / "plugin.json").write_text(
                """
                {
                  "name": "demo-plugin",
                  "skills": [
                    {"name": "plugin-skill", "path": "skill/SKILL.md"},
                    {"name": "off-skill", "path": "skill/SKILL.md", "enabled": false}
                  ],
                  "commands": [{"event": "Stop", "command": "python plugin.py"}]
                }
                """,
                encoding="utf-8",
            )
            cfg = root / ".bigcode"
            (cfg / "settings.json").write_text(
                """
                {
                  "hooks": {
                    "PreToolUse": [
                      {"matcher": "Bash", "hooks": [{"type": "command", "command": "python check.py"}]}
                    ]
                  }
                }
                """,
                encoding="utf-8",
            )
            env = {"BIGCODE_HOME": str(root / "home")}
            config = load_runtime_config(root, env=env)
            config = replace(config, skill_roots=[root / "plugins"])
            report = asyncio.run(build_doctor_report(config, probe=False, env=env))
            rendered = render_doctor_report(report)
            self.assertIn("skills:", rendered)
            self.assertIn("plugin-skill", rendered)
            self.assertIn("off-skill", rendered)
            self.assertIn("commands:", rendered)
            self.assertIn("python check.py", rendered)
            self.assertIn("plugin command registration is not supported", rendered)


if __name__ == "__main__":
    unittest.main()
