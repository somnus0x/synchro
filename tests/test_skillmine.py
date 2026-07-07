from __future__ import annotations

import json
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from skillmine import cli


def root_args(base: Path) -> list[str]:
    return [
        "--codex-root",
        str(base / "codex"),
        "--claude-root",
        str(base / "claude"),
        "--factory-root",
        str(base / "factory"),
        "--factory-plugins-root",
        str(base / "factory-plugins"),
        "--agy-root",
        str(base / "agy"),
    ]


def write_skill(root: Path, name: str, body: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(body)
    return skill_dir


def run_cli(args: list[str]) -> int:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return cli.main(args)


class ConfigRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_roots = dict(cli.TOOL_ROOTS)
        self._saved_tools = cli.TOOLS

    def tearDown(self) -> None:
        cli.TOOL_ROOTS.clear()
        cli.TOOL_ROOTS.update(self._saved_roots)
        cli.TOOLS = self._saved_tools

    def test_config_registers_custom_root_as_backup_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            lib = base / "lib" / "skills"
            write_skill(lib, "lazy-qa", "# lazy-qa\n")
            repo = base / "arsenal"

            config = base / "config.json"
            config.write_text(json.dumps({"roots": {"privatelib": str(lib)}}))

            exit_code = run_cli(
                [
                    "backup",
                    "--config",
                    str(config),
                    "--repo",
                    str(repo),
                    "--root",
                    "privatelib",
                    "--apply",
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue((repo / "skills" / "privatelib" / "lazy-qa" / "SKILL.md").exists())

    def test_config_refuses_builtin_name_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "config.json"
            config.write_text(json.dumps({"roots": {"claude": "/somewhere"}}))
            with self.assertRaises(SystemExit):
                cli.load_config_roots(str(config))

    def test_default_config_absent_returns_empty(self) -> None:
        # No explicit --config and the default file absent => optional, empty.
        with tempfile.TemporaryDirectory() as tmp:
            missing_default = Path(tmp) / "does-not-exist.json"
            original = cli.DEFAULT_CONFIG_PATH
            cli.DEFAULT_CONFIG_PATH = str(missing_default)
            try:
                self.assertEqual(cli.load_config_roots(None), {})
            finally:
                cli.DEFAULT_CONFIG_PATH = original

    def test_explicit_missing_config_errors(self) -> None:
        # An explicitly-passed --config that doesn't exist must fail loud.
        with self.assertRaises(SystemExit):
            cli.load_config_roots("/nonexistent/skillmine/config.json")


class SkillmineTests(unittest.TestCase):
    def test_sync_dry_run_does_not_copy_missing_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            codex = base / "codex"
            claude = base / "claude"
            write_skill(codex, "one", "# One\n")

            exit_code = run_cli(
                [
                    "sync",
                    "--from",
                    "codex",
                    "--to",
                    "claude",
                    *root_args(base),
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertFalse((claude / "one").exists())

    def test_sync_apply_copies_missing_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            codex = base / "codex"
            claude = base / "claude"
            write_skill(codex, "one", "# One\n")

            exit_code = run_cli(
                [
                    "sync",
                    "--from",
                    "codex",
                    "--to",
                    "claude",
                    *root_args(base),
                    "--apply",
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual((claude / "one" / "SKILL.md").read_text(), "# One\n")

    def test_conflicting_skill_is_not_replaced_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            codex = base / "codex"
            claude = base / "claude"
            write_skill(codex, "one", "# Source\n")
            write_skill(claude, "one", "# Target\n")

            exit_code = run_cli(
                [
                    "sync",
                    "--from",
                    "codex",
                    "--to",
                    "claude",
                    *root_args(base),
                    "--apply",
                ]
            )

            self.assertEqual(exit_code, 2)
            self.assertEqual((claude / "one" / "SKILL.md").read_text(), "# Target\n")

    def test_backup_apply_writes_manifest_and_skill_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            codex = base / "codex"
            claude = base / "claude"
            repo = base / "vault"
            write_skill(codex, "one", "# One\n")
            write_skill(claude, "two", "# Two\n")

            exit_code = run_cli(
                [
                    "backup",
                    "--repo",
                    str(repo),
                    *root_args(base),
                    "--apply",
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue((repo / ".git").exists())
            self.assertEqual((repo / "skills" / "codex" / "one" / "SKILL.md").read_text(), "# One\n")
            self.assertEqual((repo / "skills" / "claude" / "two" / "SKILL.md").read_text(), "# Two\n")
            manifest = json.loads((repo / "skillmine-manifest.json").read_text())
            self.assertEqual(len(manifest["skills"]), 2)

    def test_sync_supports_factory_and_agy_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            factory = base / "factory"
            agy = base / "agy"
            write_skill(factory, "three", "# Three\n")

            exit_code = run_cli(
                [
                    "sync",
                    "--from",
                    "factory",
                    "--to",
                    "agy",
                    *root_args(base),
                    "--apply",
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual((agy / "three" / "SKILL.md").read_text(), "# Three\n")

    def test_factory_discovers_droid_plugin_skills_for_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "vault"
            plugin_skill = base / "factory-plugins" / "plugins" / "core" / "skills" / "review"
            write_skill(plugin_skill.parent, "review", "# Review\n")

            exit_code = run_cli(
                [
                    "backup",
                    "--repo",
                    str(repo),
                    "--root",
                    "factory",
                    *root_args(base),
                    "--apply",
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual((repo / "skills" / "factory" / "review" / "SKILL.md").read_text(), "# Review\n")

    def test_sync_to_factory_refuses_to_force_replace_plugin_managed_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            codex = base / "codex"
            factory_plugins = base / "factory-plugins"
            write_skill(codex, "review", "# Codex Review\n")
            write_skill(factory_plugins / "plugins" / "core" / "skills", "review", "# Droid Review\n")

            exit_code = run_cli(
                [
                    "sync",
                    "--from",
                    "codex",
                    "--to",
                    "factory",
                    "--name",
                    "review",
                    *root_args(base),
                    "--force",
                    "--apply",
                ]
            )

            self.assertEqual(exit_code, 2)
            self.assertFalse((base / "factory" / "review").exists())

    def test_backup_can_select_agy_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            agy = base / "agy"
            repo = base / "vault"
            write_skill(agy, "four", "# Four\n")

            exit_code = run_cli(
                [
                    "backup",
                    "--repo",
                    str(repo),
                    "--root",
                    "agy",
                    *root_args(base),
                    "--apply",
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual((repo / "skills" / "agy" / "four" / "SKILL.md").read_text(), "# Four\n")

    def test_restore_apply_copies_skill_from_vault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "vault"
            claude = base / "claude"
            write_skill(repo / "skills" / "codex", "one", "# One\n")

            exit_code = run_cli(
                [
                    "restore",
                    "--repo",
                    str(repo),
                    "--from",
                    "codex",
                    "--to",
                    "claude",
                    *root_args(base),
                    "--apply",
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual((claude / "one" / "SKILL.md").read_text(), "# One\n")

    def test_restore_refuses_conflict_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "vault"
            claude = base / "claude"
            write_skill(repo / "skills" / "codex", "one", "# Backup\n")
            write_skill(claude, "one", "# Local\n")

            exit_code = run_cli(
                [
                    "restore",
                    "--repo",
                    str(repo),
                    "--from",
                    "codex",
                    "--to",
                    "claude",
                    *root_args(base),
                    "--apply",
                ]
            )

            self.assertEqual(exit_code, 2)
            self.assertEqual((claude / "one" / "SKILL.md").read_text(), "# Local\n")

    def test_doctor_reports_configured_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_skill(base / "agy", "four", "# Four\n")

            exit_code = run_cli(["doctor", *root_args(base)])

            self.assertEqual(exit_code, 0)

    def test_backup_excludes_local_and_secret_like_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            codex = base / "codex"
            claude = base / "claude"
            repo = base / "vault"
            skill = write_skill(codex, "one", "# One\n")
            (skill / ".envrc").write_text("SECRET=1\n")
            (skill / "settings.local.json").write_text("{}\n")

            exit_code = run_cli(
                [
                    "backup",
                    "--repo",
                    str(repo),
                    "--root",
                    "codex",
                    *root_args(base),
                    "--apply",
                ]
            )

            self.assertEqual(exit_code, 0)
            backed_up = repo / "skills" / "codex" / "one"
            self.assertTrue((backed_up / "SKILL.md").exists())
            self.assertFalse((backed_up / ".envrc").exists())
            self.assertFalse((backed_up / "settings.local.json").exists())


if __name__ == "__main__":
    unittest.main()
