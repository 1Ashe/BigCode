from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

from bigcode.config import load_runtime_config
from bigcode.config.models import McpServerConfig
from bigcode.diagnostics import build_doctor_report, render_doctor_report


def _write_model_config(root: Path, *, api_key_env: str | None = "TEST_API_KEY") -> None:
    cfg = root / ".bigcode"
    cfg.mkdir()
    api_key_line = f'"api_key_env": "{api_key_env}",' if api_key_env else ""
    (cfg / "models.json").write_text(
        f"""
        {{
          "default_model": "local:test",
          "providers": {{
            "local": {{
              "base_url": "https://api.example.test/v1",
              {api_key_line}
              "models": {{"test": {{"id": "test-model"}}}}
            }}
          }}
        }}
        """,
        encoding="utf-8",
    )


class DiagnosticsTests(unittest.TestCase):
    def test_doctor_reports_missing_model_registry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = {"BIGCODE_HOME": str(root / "home")}
            config = load_runtime_config(root, env=env)
            report = asyncio.run(build_doctor_report(config, probe=False, env=env))
            self.assertTrue(report.has_errors)
            self.assertTrue(any(item.category == "provider" and item.status == "ERROR" for item in report.items))
            self.assertIn("no models", render_doctor_report(report))

    def test_doctor_reports_missing_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_model_config(root, api_key_env="TEST_API_KEY")
            env = {"BIGCODE_HOME": str(root / "home")}
            config = load_runtime_config(root, env=env)
            report = asyncio.run(build_doctor_report(config, probe=False, env=env))
            self.assertTrue(report.has_errors)
            self.assertTrue(any(item.name == "api key" and "TEST_API_KEY" in item.message for item in report.items))

    def test_doctor_reports_sandbox_profile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_model_config(root, api_key_env=None)
            cfg = root / ".bigcode"
            (cfg / "settings.json").write_text('{"sandbox": {"profile": "read-only"}}', encoding="utf-8")
            env = {"BIGCODE_HOME": str(root / "home")}
            config = load_runtime_config(root, env=env)
            report = asyncio.run(build_doctor_report(config, probe=False, env=env))
            rendered = render_doctor_report(report)
            self.assertIn("sandbox profile", rendered)
            self.assertIn("read-only", rendered)

    def test_provider_probe_uses_minimal_claude_messages_request(self) -> None:
        calls: dict[str, Any] = {}

        class FakeAsyncClient:
            def __init__(self, *, timeout: float) -> None:
                calls["timeout"] = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]):
                calls["url"] = url
                calls["headers"] = headers
                calls["json"] = json
                return httpx.Response(200, json={"content": []}, request=httpx.Request("POST", url))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_model_config(root, api_key_env="TEST_API_KEY")
            env = {"BIGCODE_HOME": str(root / "home"), "TEST_API_KEY": "secret"}
            config = load_runtime_config(root, env=env)
            with patch("bigcode.diagnostics.httpx.AsyncClient", FakeAsyncClient):
                report = asyncio.run(build_doctor_report(config, probe=True, timeout=3, env=env))
            self.assertFalse(report.has_errors)
            self.assertEqual(calls["timeout"], 3)
            self.assertEqual(calls["url"], "https://api.example.test/v1/messages")
            self.assertEqual(calls["headers"]["x-api-key"], "secret")
            self.assertEqual(calls["json"]["max_tokens"], 1)
            self.assertEqual(calls["json"]["messages"][0]["content"][0]["text"], "ping")

    def test_no_probe_does_not_call_provider(self) -> None:
        class FailingAsyncClient:
            def __init__(self, *, timeout: float) -> None:
                raise AssertionError("provider probe should not run")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_model_config(root, api_key_env="TEST_API_KEY")
            env = {"BIGCODE_HOME": str(root / "home"), "TEST_API_KEY": "secret"}
            config = load_runtime_config(root, env=env)
            with patch("bigcode.diagnostics.httpx.AsyncClient", FailingAsyncClient):
                report = asyncio.run(build_doctor_report(config, probe=False, env=env))
            self.assertFalse(report.has_errors)
            self.assertTrue(any(item.name == "probe" and "disabled" in item.message for item in report.items))

    def test_doctor_reports_skill_load_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_model_config(root, api_key_env=None)
            skill = root / "skills" / "bad"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: BadName\n---\nBad.", encoding="utf-8")
            env = {"BIGCODE_HOME": str(root / "home")}
            config = load_runtime_config(root, env=env)
            config = replace(config, skill_roots=[root / "skills"])
            report = asyncio.run(build_doctor_report(config, probe=False, env=env))
            self.assertTrue(any(item.category == "skills" and item.status == "WARN" and "invalid skill name" in item.message for item in report.items))

    def test_doctor_reports_fastmcp_missing(self) -> None:
        class FakeMcpManager:
            fastmcp_available = False

            async def discover(self, server_name: str | None = None):
                raise AssertionError("discover should not run without FastMCP")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_model_config(root, api_key_env=None)
            env = {"BIGCODE_HOME": str(root / "home")}
            config = load_runtime_config(root, env=env)
            config = replace(
                config,
                mcp_servers={"demo": McpServerConfig(name="demo", config={"transport": "stdio"}, enabled=True)},
                mcp_enabled=True,
            )
            report = asyncio.run(build_doctor_report(config, probe=True, env=env, mcp_manager=FakeMcpManager()))
            self.assertTrue(any(item.category == "mcp" and item.name == "fastmcp" and item.status == "WARN" for item in report.items))


if __name__ == "__main__":
    unittest.main()
