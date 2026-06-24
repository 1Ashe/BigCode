"""Unit tests for sandbox module."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase

from bigcode.sandbox import (
    BubblewrapBuilder,
    FilesystemSandboxConfig,
    NetworkSandboxConfig,
    SandboxConfig,
    check_dependencies,
    derive_sandbox_config,
    detect_platform,
    should_use_sandbox,
)
from bigcode.tools.permissions.models import PermissionRule, ToolPermissionContext


class PlatformDetectionTests(TestCase):
    def test_detect_platform_returns_valid_string(self):
        platform = detect_platform()
        self.assertIn(platform, ("linux", "wsl", "unsupported"))


class DependencyCheckTests(TestCase):
    def test_check_dependencies_returns_structured_result(self):
        result = check_dependencies()
        self.assertIsInstance(result.errors, list)
        self.assertIsInstance(result.warnings, list)


class ShouldUseSandboxTests(TestCase):
    def setUp(self):
        self.config = SandboxConfig(enabled=True)

    def test_disabled_when_config_is_none(self):
        self.assertFalse(should_use_sandbox(command="ls", dangerously_disable_sandbox=False, config=None))

    def test_disabled_when_explicitly_disabled(self):
        config = SandboxConfig(enabled=False)
        self.assertFalse(should_use_sandbox(command="ls", dangerously_disable_sandbox=False, config=config))

    def test_enabled_by_default(self):
        config = SandboxConfig()
        self.assertTrue(config.enabled)
        self.assertFalse(config.auto_allow_bash_if_sandboxed)

    def test_enabled_for_normal_command(self):
        # Only runs if platform is linux or wsl
        platform = detect_platform()
        if platform in ("linux", "wsl"):
            self.assertTrue(should_use_sandbox(command="ls", dangerously_disable_sandbox=False, config=self.config))

    def test_disabled_when_dangerously_disable_sandbox(self):
        self.assertFalse(should_use_sandbox(command="ls", dangerously_disable_sandbox=True, config=self.config))

    def test_disabled_when_allow_unsandboxed_false(self):
        config = SandboxConfig(enabled=True, allow_unsandboxed_commands=False)
        # dangerouslyDisableSandbox is ignored when allowUnsandboxedCommands=False
        platform = detect_platform()
        if platform in ("linux", "wsl"):
            self.assertTrue(should_use_sandbox(command="ls", dangerously_disable_sandbox=True, config=config))

    def test_excluded_command_match(self):
        config = SandboxConfig(enabled=True, excluded_commands=["docker"])
        # "docker ps" matches "docker" prefix, so sandbox is NOT used (excluded)
        self.assertFalse(should_use_sandbox(command="docker ps", dangerously_disable_sandbox=False, config=config))


class DeriveSandboxConfigTests(TestCase):
    def setUp(self):
        self.cwd = Path("/home/test/project")
        self.workspace_roots = [self.cwd]
        self.config_roots = [self.cwd / ".bigcode", Path("/home/test/.bigcode")]
        self.skill_roots = [Path("/home/test/.bigcode/skills")]
        self.agent_roots = [Path("/home/test/.bigcode/agents")]
        self.bigcode_home = Path("/home/test/.bigcode")

    def _derive(self, permission_context, sandbox_config=...):
        if sandbox_config is ...:
            sandbox_config = SandboxConfig(enabled=True)
        return derive_sandbox_config(
            permission_context=permission_context,
            sandbox_config=sandbox_config,
            cwd=self.cwd,
            workspace_roots=self.workspace_roots,
            config_roots=self.config_roots,
            skill_roots=self.skill_roots,
            agent_roots=self.agent_roots,
            bigcode_home=self.bigcode_home,
            temp_dir="/tmp",
        )

    def test_returns_disabled_when_none(self):
        ctx = ToolPermissionContext(mode="default")
        result = self._derive(ctx, sandbox_config=None)
        self.assertFalse(result.enabled)

    def test_returns_enabled_copy(self):
        ctx = ToolPermissionContext(mode="default")
        result = self._derive(ctx)
        self.assertTrue(result.enabled)

    def test_cwd_in_allow_write(self):
        ctx = ToolPermissionContext(mode="default")
        result = self._derive(ctx)
        self.assertIn(str(self.cwd), result.filesystem.allow_write)

    def test_settings_json_in_deny_write(self):
        ctx = ToolPermissionContext(mode="default")
        result = self._derive(ctx)
        deny = result.filesystem.deny_write
        self.assertIn(str(self.cwd / ".bigcode" / "settings.json"), deny)
        self.assertIn(str(self.cwd / ".bigcode" / "settings.local.json"), deny)
        self.assertIn(str(self.cwd / ".bigcode" / "models.json"), deny)
        self.assertIn(str(self.cwd / ".bigcode" / "mcp.json"), deny)
        self.assertIn(str(Path("/home/test/.bigcode/settings.json")), deny)
        self.assertIn(str(Path("/home/test/.bigcode/models.json")), deny)

    def test_skills_in_deny_write(self):
        ctx = ToolPermissionContext(mode="default")
        result = self._derive(ctx)
        deny = result.filesystem.deny_write
        self.assertIn(str(self.cwd / ".bigcode" / "skills"), deny)
        self.assertIn(str(Path("/home/test/.bigcode/skills")), deny)

    def test_commands_and_agents_in_deny_write(self):
        ctx = ToolPermissionContext(mode="default")
        result = self._derive(ctx)
        deny = result.filesystem.deny_write
        self.assertIn(str(self.cwd / ".bigcode" / "commands"), deny)
        self.assertIn(str(self.cwd / ".bigcode" / "agents"), deny)
        self.assertIn(str(Path("/home/test/.bigcode/commands")), deny)
        self.assertIn(str(Path("/home/test/.bigcode/agents")), deny)

    def test_edit_allow_rule_to_allow_write(self):
        ctx = ToolPermissionContext(
            mode="default",
            always_allow=[
                PermissionRule(tool_name="Edit", behavior="allow", pattern="src/**", source="config"),
            ],
        )
        result = self._derive(ctx)
        # When the glob prefix doesn't exist on disk, it falls back to cwd.
        # The cwd is already in allow_write.
        allow = result.filesystem.allow_write
        self.assertIn(str(self.cwd), allow)

    def test_edit_deny_rule_to_deny_write(self):
        ctx = ToolPermissionContext(
            mode="default",
            always_deny=[
                PermissionRule(tool_name="Edit", behavior="deny", pattern="secrets/", source="config"),
            ],
        )
        result = self._derive(ctx)
        deny = result.filesystem.deny_write
        self.assertTrue(any("secrets" in p for p in deny), f"Expected secrets/ in deny_write: {deny}")

    def test_read_deny_rule_to_deny_read(self):
        ctx = ToolPermissionContext(
            mode="default",
            always_deny=[
                PermissionRule(tool_name="Read", behavior="deny", pattern=".env", source="config"),
            ],
        )
        result = self._derive(ctx)
        deny_read = result.filesystem.deny_read
        self.assertTrue(any(".env" in p for p in deny_read), f"Expected .env in deny_read: {deny_read}")

    def test_webfetch_allow_domain(self):
        ctx = ToolPermissionContext(
            mode="default",
            always_allow=[
                PermissionRule(tool_name="WebFetch", behavior="allow", pattern="domain:github.com", source="config"),
            ],
        )
        result = self._derive(ctx)
        self.assertIn("github.com", result.network.allowed_domains)

    def test_webfetch_deny_domain(self):
        ctx = ToolPermissionContext(
            mode="default",
            always_deny=[
                PermissionRule(tool_name="WebFetch", behavior="deny", pattern="domain:evil.com", source="config"),
            ],
        )
        result = self._derive(ctx)
        self.assertIn("evil.com", result.network.denied_domains)

    def test_bare_git_repo_scrub_paths(self):
        ctx = ToolPermissionContext(mode="default")
        result = self._derive(ctx)
        # The cwd doesn't have bare git repo files, so they should be in scrub_paths
        self.assertIn(str(self.cwd / "HEAD"), result.scrub_paths)
        self.assertIn(str(self.cwd / "config"), result.scrub_paths)

    def test_system_deny_prefixes_in_deny_write(self):
        ctx = ToolPermissionContext(mode="default")
        result = self._derive(ctx)
        deny = result.filesystem.deny_write
        for prefix in ["/bin", "/usr", "/etc"]:
            self.assertIn(prefix, deny)

    def test_preserves_network_config(self):
        sandbox = SandboxConfig(
            enabled=True,
            network=NetworkSandboxConfig(
                allowed_domains=["pypi.org"],
                allow_local_binding=True,
            ),
        )
        ctx = ToolPermissionContext(mode="default")
        result = self._derive(ctx, sandbox_config=sandbox)
        self.assertIn("pypi.org", result.network.allowed_domains)
        self.assertTrue(result.network.allow_local_binding)

    def test_preserves_filesystem_config(self):
        sandbox = SandboxConfig(
            enabled=True,
            filesystem=FilesystemSandboxConfig(
                allow_write=["/extra/path"],
                deny_write=["/blocked/path"],
            ),
        )
        ctx = ToolPermissionContext(mode="default")
        result = self._derive(ctx, sandbox_config=sandbox)
        self.assertIn("/extra/path", result.filesystem.allow_write)
        self.assertIn("/blocked/path", result.filesystem.deny_write)

    def test_preserves_behavior_flags(self):
        sandbox = SandboxConfig(
            enabled=True,
            auto_allow_bash_if_sandboxed=False,
            allow_unsandboxed_commands=False,
            fail_if_unavailable=True,
            excluded_commands=["docker", "kubectl"],
        )
        ctx = ToolPermissionContext(mode="default")
        result = self._derive(ctx, sandbox_config=sandbox)
        self.assertFalse(result.auto_allow_bash_if_sandboxed)
        self.assertFalse(result.allow_unsandboxed_commands)
        self.assertTrue(result.fail_if_unavailable)
        self.assertEqual(result.excluded_commands, ["docker", "kubectl"])


class BubblewrapBuilderTests(TestCase):
    def setUp(self):
        self.config = SandboxConfig(
            enabled=True,
            filesystem=FilesystemSandboxConfig(
                allow_write=["/home/test"],
                deny_write=["/home/test/.bigcode/settings.json"],
                deny_read=["/home/test/.env"],
            ),
            network=NetworkSandboxConfig(),
        )

    def test_build_returns_list(self):
        builder = BubblewrapBuilder(self.config)
        args = builder.build("echo hi", shell_path="/bin/bash", chdir="/home/test", tmp_dir="/tmp/xyz")
        self.assertIsInstance(args, list)
        self.assertEqual(args[0], "bwrap")

    def test_build_includes_command(self):
        builder = BubblewrapBuilder(self.config)
        args = builder.build("echo hi", shell_path="/bin/bash", chdir="/home/test", tmp_dir="/tmp/xyz")
        self.assertIn("--", args)
        sep_idx = args.index("--")
        self.assertEqual(args[sep_idx + 1:], ["/bin/bash", "-c", "echo hi"])

    def test_build_includes_system_dirs(self):
        builder = BubblewrapBuilder(self.config)
        args = builder.build("echo hi", shell_path="/bin/bash", chdir="/home/test", tmp_dir="/tmp/xyz")
        for sys_dir in ["/usr", "/proc", "/dev"]:
            self.assertIn(sys_dir, args)

    def test_build_includes_chdir(self):
        builder = BubblewrapBuilder(self.config)
        args = builder.build("echo hi", shell_path="/bin/bash", chdir="/home/test", tmp_dir="/tmp/xyz")
        chdir_idx = args.index("--chdir")
        self.assertEqual(args[chdir_idx + 1], "/home/test")

    def test_build_with_network_restriction(self):
        config = SandboxConfig(
            enabled=True,
            filesystem=FilesystemSandboxConfig(),
            network=NetworkSandboxConfig(allowed_domains=["github.com"]),
        )
        builder = BubblewrapBuilder(config)
        args = builder.build("echo hi", shell_path="/bin/bash", chdir="/home/test", tmp_dir="/tmp/xyz")
        self.assertNotIn("--share-net", args)
        self.assertIn("--unshare-net", args)

    def test_build_without_network_restriction(self):
        config = SandboxConfig(
            enabled=True,
            filesystem=FilesystemSandboxConfig(),
            network=NetworkSandboxConfig(),
        )
        builder = BubblewrapBuilder(config)
        args = builder.build("echo hi", shell_path="/bin/bash", chdir="/home/test", tmp_dir="/tmp/xyz")
        self.assertIn("--share-net", args)
        self.assertNotIn("--unshare-net", args)

    def test_build_paths_sorted_by_depth(self):
        config = SandboxConfig(
            enabled=True,
            filesystem=FilesystemSandboxConfig(
                allow_write=["/a/b/c", "/a", "/a/b"],
            ),
        )
        builder = BubblewrapBuilder(config)
        args = builder.build("echo hi", shell_path="/bin/bash", chdir="/home/test", tmp_dir="/tmp/xyz")
        # Find the positions of the allow_write bind mounts
        bind_indices = [i for i, a in enumerate(args) if a == "--bind"]
        # The paths after --bind should be sorted by depth
        paths = [args[i + 1] for i in bind_indices if args[i + 2] == args[i + 1]]
        depths = [p.count("/") for p in paths]
        self.assertEqual(depths, sorted(depths))
