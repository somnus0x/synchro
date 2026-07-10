from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from synchro import cli


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

    def test_config_rejects_non_object_top_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text("[]")

            with self.assertRaises(SystemExit):
                cli.load_config_roots(str(config))

    def test_config_rejects_unsafe_and_reserved_root_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            for name in (
                "../../escaped",
                "/tmp/escaped",
                "all",
                "ALL",
                "Codex",
                "factory_plugins",
                "factory-plugins",
            ):
                with self.subTest(name=name):
                    config.write_text(json.dumps({"roots": {name: "/tmp/skills"}}))
                    with self.assertRaises(SystemExit):
                        cli.load_config_roots(str(config))

    def test_config_rejects_custom_root_names_that_collide_by_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({
                "roots": {"Private": "/tmp/one", "private": "/tmp/two"},
            }))

            with self.assertRaises(SystemExit):
                cli.load_config_roots(str(config))

    def test_hyphenated_custom_root_can_be_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            library = base / "library"
            repo = base / "vault"
            write_skill(library, "one", "# One\n")
            config = base / "config.json"
            config.write_text(json.dumps({"roots": {"private-lib": str(library)}}))

            exit_code = run_cli([
                "backup", "--config", str(config), "--repo", str(repo),
                "--root", "private-lib", "--apply",
            ])

            self.assertEqual(exit_code, 0)
            self.assertTrue((repo / "skills" / "private-lib" / "one" / "SKILL.md").exists())

    def test_default_config_absent_returns_empty(self) -> None:
        # No explicit --config and the default file absent => optional, empty.
        with tempfile.TemporaryDirectory() as tmp:
            missing_default = Path(tmp) / "does-not-exist.json"
            missing_legacy = Path(tmp) / "legacy-does-not-exist.json"
            original_default = cli.DEFAULT_CONFIG_PATH
            original_legacy = cli.LEGACY_CONFIG_PATH
            cli.DEFAULT_CONFIG_PATH = str(missing_default)
            cli.LEGACY_CONFIG_PATH = str(missing_legacy)
            try:
                self.assertEqual(cli.load_config_roots(None), {})
            finally:
                cli.DEFAULT_CONFIG_PATH = original_default
                cli.LEGACY_CONFIG_PATH = original_legacy

    def test_default_config_falls_back_to_legacy_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            legacy = base / "legacy-config.json"
            legacy.write_text(json.dumps({"roots": {"library": "/tmp/library"}}))
            original_default = cli.DEFAULT_CONFIG_PATH
            original_legacy = cli.LEGACY_CONFIG_PATH
            cli.DEFAULT_CONFIG_PATH = str(base / "missing-new-config.json")
            cli.LEGACY_CONFIG_PATH = str(legacy)
            try:
                self.assertEqual(cli.load_config_roots(None), {"library": "/tmp/library"})
            finally:
                cli.DEFAULT_CONFIG_PATH = original_default
                cli.LEGACY_CONFIG_PATH = original_legacy

    def test_new_default_config_wins_over_legacy_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            current = base / "config.json"
            legacy = base / "legacy-config.json"
            current.write_text(json.dumps({"roots": {"current": "/tmp/current"}}))
            legacy.write_text(json.dumps({"roots": {"legacy": "/tmp/legacy"}}))
            original_default = cli.DEFAULT_CONFIG_PATH
            original_legacy = cli.LEGACY_CONFIG_PATH
            cli.DEFAULT_CONFIG_PATH = str(current)
            cli.LEGACY_CONFIG_PATH = str(legacy)
            try:
                self.assertEqual(cli.load_config_roots(None), {"current": "/tmp/current"})
            finally:
                cli.DEFAULT_CONFIG_PATH = original_default
                cli.LEGACY_CONFIG_PATH = original_legacy

    def test_explicit_missing_config_errors(self) -> None:
        # An explicitly-passed --config that doesn't exist must fail loud.
        with self.assertRaises(SystemExit):
            cli.load_config_roots("/nonexistent/synchro/config.json")


class ManifestMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_roots = dict(cli.TOOL_ROOTS)
        self._saved_tools = cli.TOOLS

    def tearDown(self) -> None:
        cli.TOOL_ROOTS.clear()
        cli.TOOL_ROOTS.update(self._saved_roots)
        cli.TOOLS = self._saved_tools

    def test_merge_preserves_other_root_and_overwrites_same_dest(self) -> None:
        existing = {
            "roots": {"claude": "/Users/isagi/.claude/skills"},
            "skills": [
                {"tool": "claude", "name": "adapt", "source": "/Users/isagi/.claude/skills/adapt",
                 "dest": "skills/claude/adapt", "sha256": "old-mac"},
                {"tool": "thufir", "name": "occam", "source": "/root/thufir-skills/occam",
                 "dest": "skills/thufir/occam", "sha256": "stale"},
            ],
        }
        new_roots = {"thufir": "/root/thufir-skills"}
        new_skills = [
            {"tool": "thufir", "name": "occam", "source": "/root/thufir-skills/occam",
             "dest": "skills/thufir/occam", "sha256": "fresh"},
        ]
        merged = cli.merge_manifest(existing, new_roots, new_skills)

        dests = {e["dest"]: e for e in merged["skills"]}
        self.assertEqual(dests["skills/claude/adapt"]["sha256"], "old-mac")
        self.assertEqual(dests["skills/thufir/occam"]["sha256"], "fresh")
        self.assertEqual(
            merged["roots"],
            {"claude": "/Users/isagi/.claude/skills", "thufir": "/root/thufir-skills"},
        )

    def test_partial_backup_does_not_drop_prior_manifest_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "vault"
            repo.mkdir()
            (repo / cli.MANIFEST_NAME).write_text(json.dumps({
                "roots": {"claude": "/other/claude"},
                "skills": [
                    {"tool": "claude", "name": "a", "source": "/other/claude/a",
                     "dest": "skills/claude/a", "sha256": "x"},
                    {"tool": "claude", "name": "b", "source": "/other/claude/b",
                     "dest": "skills/claude/b", "sha256": "y"},
                ],
            }))
            lib = base / "lib" / "skills"
            write_skill(lib, "lazy-qa", "# lazy-qa\n")
            config = base / "config.json"
            config.write_text(json.dumps({"roots": {"privatelib": str(lib)}}))

            exit_code = run_cli([
                "backup", "--config", str(config), "--repo", str(repo),
                "--root", "privatelib", "--apply",
            ])
            self.assertEqual(exit_code, 0)

            manifest = json.loads((repo / cli.MANIFEST_NAME).read_text())
            dests = {e["dest"] for e in manifest["skills"]}
            self.assertIn("skills/claude/a", dests)
            self.assertIn("skills/claude/b", dests)
            self.assertTrue(any(d.endswith("privatelib/lazy-qa") for d in dests))

    def test_backup_normalizes_absolute_manifest_destinations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "vault"
            repo.mkdir()
            (repo / cli.MANIFEST_NAME).write_text(json.dumps({
                "roots": {"codex": "/old/.codex/skills"},
                "skills": [
                    {"tool": "codex", "name": "one", "source": "/old/.codex/skills/one",
                     "dest": "/Users/old/Development/arsenal/skills/codex/one",
                     "sha256": "old"},
                ],
            }))
            codex = base / "codex"
            write_skill(codex, "one", "# One\n")

            exit_code = run_cli([
                "backup", "--repo", str(repo), "--root", "codex",
                *root_args(base), "--apply",
            ])
            self.assertEqual(exit_code, 0)

            manifest = json.loads((repo / cli.MANIFEST_NAME).read_text())
            self.assertEqual(
                [entry["dest"] for entry in manifest["skills"]],
                ["skills/codex/one"],
            )
            self.assertNotEqual(manifest["skills"][0]["sha256"], "old")

    def test_normalizes_windows_manifest_destination_on_posix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "vault"
            normalized = cli.normalize_manifest_dest(
                r"C:\Users\somnus\agent-vault\skills\codex\one",
                repo,
            )

            self.assertEqual(normalized, "skills/codex/one")

    def test_backup_recovers_from_invalid_manifest_field_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "vault"
            repo.mkdir()
            (repo / cli.MANIFEST_NAME).write_text(
                json.dumps({"roots": None, "skills": None})
            )
            write_skill(base / "codex", "one", "# One\n")

            exit_code = run_cli([
                "backup", "--repo", str(repo), "--root", "codex",
                *root_args(base), "--apply",
            ])

            self.assertEqual(exit_code, 0)
            manifest = json.loads((repo / cli.MANIFEST_NAME).read_text())
            self.assertIsInstance(manifest["roots"], dict)
            self.assertEqual([entry["dest"] for entry in manifest["skills"]], ["skills/codex/one"])

    def test_backup_migrates_legacy_manifest_without_losing_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "vault"
            repo.mkdir()
            legacy_manifest = repo / cli.LEGACY_MANIFEST_NAME
            legacy_manifest.write_text(json.dumps({
                "roots": {"claude": "/old/claude"},
                "skills": [{
                    "tool": "claude",
                    "name": "old",
                    "source": "/old/claude/old",
                    "dest": "skills/claude/old",
                    "sha256": "old-digest",
                }],
            }))
            write_skill(base / "codex", "one", "# One\n")

            exit_code = run_cli([
                "backup", "--repo", str(repo), "--root", "codex",
                *root_args(base), "--apply",
            ])

            self.assertEqual(exit_code, 0)
            self.assertFalse(legacy_manifest.exists())
            manifest = json.loads((repo / cli.MANIFEST_NAME).read_text())
            self.assertEqual(
                {entry["dest"] for entry in manifest["skills"]},
                {"skills/claude/old", "skills/codex/one"},
            )

    def test_current_manifest_wins_when_both_manifest_versions_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "vault"
            repo.mkdir()
            (repo / cli.LEGACY_MANIFEST_NAME).write_text(json.dumps({
                "roots": {"codex": "/legacy"},
                "skills": [{"dest": "skills/codex/one", "sha256": "legacy"}],
            }))
            (repo / cli.MANIFEST_NAME).write_text(json.dumps({
                "roots": {"codex": "/current"},
                "skills": [{"dest": "skills/codex/one", "sha256": "current"}],
            }))

            manifest = cli.load_existing_manifest(repo)

            self.assertEqual(manifest["roots"], {"codex": "/current"})
            self.assertEqual(manifest["skills"], [{"dest": "skills/codex/one", "sha256": "current"}])

    def test_invalid_legacy_manifest_is_not_deleted_during_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "vault"
            repo.mkdir()
            legacy_manifest = repo / cli.LEGACY_MANIFEST_NAME
            legacy_manifest.write_text(json.dumps({"roots": None, "skills": None}))
            write_skill(base / "codex", "one", "# One\n")

            exit_code = run_cli([
                "backup", "--repo", str(repo), "--root", "codex",
                *root_args(base), "--apply",
            ])

            self.assertEqual(exit_code, 0)
            self.assertTrue(legacy_manifest.exists())
            self.assertTrue((repo / cli.MANIFEST_NAME).exists())


class SynchroTests(unittest.TestCase):
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

    def test_sync_requested_missing_skill_returns_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_skill(base / "codex", "one", "# One\n")

            exit_code = run_cli([
                "sync", "--from", "codex", "--to", "claude",
                "--name", "absent", *root_args(base),
            ])

            self.assertEqual(exit_code, 1)

    def test_sync_missing_source_root_returns_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            exit_code = run_cli([
                "sync", "--from", "codex", "--to", "claude",
                *root_args(base),
            ])

            self.assertEqual(exit_code, 1)

    def test_sync_replaces_file_target_and_backs_it_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_skill(base / "codex", "one", "# One\n")
            claude = base / "claude"
            claude.mkdir()
            (claude / "one").write_text("blocking file\n")
            backup_dir = base / "safety-backups"

            exit_code = run_cli([
                "sync", "--from", "codex", "--to", "claude",
                "--backup-dir", str(backup_dir), *root_args(base), "--apply",
            ])

            self.assertEqual(exit_code, 0)
            self.assertEqual((claude / "one" / "SKILL.md").read_text(), "# One\n")
            backups = list(backup_dir.glob("*/claude/one"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), "blocking file\n")

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
            manifest = json.loads((repo / cli.MANIFEST_NAME).read_text())
            self.assertEqual(len(manifest["skills"]), 2)

    def test_backup_preserves_symlinks_without_copying_external_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "vault"
            external = base / ".env.production"
            external.write_text("TOP_SECRET=1\n")
            skill = write_skill(base / "codex", "linked", "# Linked\n")
            (skill / "reference.txt").symlink_to(external)
            (skill / "loop").symlink_to(".", target_is_directory=True)

            exit_code = run_cli([
                "backup", "--repo", str(repo), "--root", "codex",
                *root_args(base), "--apply",
            ])

            self.assertEqual(exit_code, 0)
            backed_up = repo / "skills" / "codex" / "linked"
            self.assertTrue((backed_up / "reference.txt").is_symlink())
            self.assertTrue((backed_up / "loop").is_symlink())

    def test_backup_rejects_top_level_skill_symlink_outside_source_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            codex = base / "codex"
            codex.mkdir()
            external = write_skill(base, "private-skill", "# Private\n")
            (external / "credentials.json").write_text('{"token":"secret"}\n')
            (codex / "alias").symlink_to(external, target_is_directory=True)
            repo = base / "vault"

            exit_code = run_cli([
                "backup", "--repo", str(repo), "--root", "codex",
                *root_args(base), "--apply",
            ])

            self.assertEqual(exit_code, 1)
            self.assertFalse((repo / "skills" / "codex" / "alias").exists())

    def test_backup_rejects_symlinked_vault_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "vault"
            external = base / "outside"
            repo.mkdir()
            external.mkdir()
            (repo / "skills").symlink_to(external, target_is_directory=True)
            write_skill(base / "codex", "one", "# One\n")

            exit_code = run_cli([
                "backup", "--repo", str(repo), "--root", "codex",
                *root_args(base), "--apply",
            ])

            self.assertEqual(exit_code, 1)
            self.assertFalse((external / "codex" / "one").exists())

    def test_backup_rejects_symlinked_skill_destination_inside_vault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "vault"
            victim = write_skill(repo / "skills" / "claude", "victim", "# Victim\n")
            codex_vault = repo / "skills" / "codex"
            codex_vault.mkdir()
            (codex_vault / "one").symlink_to(victim, target_is_directory=True)
            write_skill(base / "codex", "one", "# Source\n")

            exit_code = run_cli([
                "backup", "--repo", str(repo), "--root", "codex",
                *root_args(base), "--apply",
            ])

            self.assertEqual(exit_code, 1)
            self.assertEqual((victim / "SKILL.md").read_text(), "# Victim\n")
            self.assertTrue((codex_vault / "one").is_symlink())

    def test_backup_rejects_source_destination_overlap_without_deleting_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "vault"
            source_root = repo / "skills" / "codex"
            skill = write_skill(source_root, "one", "# One\n")

            exit_code = run_cli([
                "backup", "--repo", str(repo), "--root", "codex",
                *root_args(base), "--codex-root", str(source_root), "--apply",
            ])

            self.assertEqual(exit_code, 1)
            self.assertEqual((skill / "SKILL.md").read_text(), "# One\n")

    def test_backup_rejects_case_alias_overlap_on_case_insensitive_filesystems(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "Vault"
            repo.mkdir()
            case_alias = base / "vault"
            try:
                same_directory = os.path.samefile(repo, case_alias)
            except OSError:
                same_directory = False
            if not same_directory:
                self.skipTest("filesystem is case-sensitive")

            source_root = repo / "skills" / "codex"
            skill = write_skill(source_root, "one", "# One\n")
            (skill / ".env.local").write_text("SECRET=1\n")

            exit_code = run_cli([
                "backup", "--repo", str(case_alias), "--root", "codex",
                *root_args(base), "--codex-root", str(source_root), "--apply",
            ])

            self.assertEqual(exit_code, 1)
            self.assertTrue((skill / ".env.local").exists())

    def test_backup_commit_works_without_a_skills_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "vault"
            (base / "codex").mkdir()
            cli.ensure_git_repo(repo)
            cli.run_git(repo, "config", "user.name", "Synchro Tests")
            cli.run_git(repo, "config", "user.email", "synchro@example.invalid")

            exit_code = run_cli([
                "backup", "--repo", str(repo), "--root", "codex",
                *root_args(base), "--apply", "--commit",
            ])

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                cli.run_git(repo, "log", "-1", "--pretty=%s").stdout.strip(),
                "backup agent skills",
            )

    def test_backup_commit_records_legacy_manifest_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "vault"
            cli.ensure_git_repo(repo)
            cli.run_git(repo, "config", "user.name", "Synchro Tests")
            cli.run_git(repo, "config", "user.email", "synchro@example.invalid")
            (repo / cli.LEGACY_MANIFEST_NAME).write_text(
                json.dumps({"roots": {}, "skills": []})
            )
            cli.run_git(repo, "add", cli.LEGACY_MANIFEST_NAME)
            cli.run_git(repo, "commit", "-m", "legacy manifest")
            write_skill(base / "codex", "one", "# One\n")

            exit_code = run_cli([
                "backup", "--repo", str(repo), "--root", "codex",
                *root_args(base), "--apply", "--commit",
            ])

            self.assertEqual(exit_code, 0)
            changed = cli.run_git(repo, "show", "--name-status", "--format=").stdout
            self.assertIn(cli.LEGACY_MANIFEST_NAME, changed)
            self.assertIn(cli.MANIFEST_NAME, changed)
            self.assertEqual(cli.run_git(repo, "status", "--short").stdout, "")

    def test_backup_commit_leaves_unrelated_staged_files_out_of_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "vault"
            cli.ensure_git_repo(repo)
            cli.run_git(repo, "config", "user.name", "Synchro Tests")
            cli.run_git(repo, "config", "user.email", "synchro@example.invalid")
            (repo / ".gitignore").write_text("\n")
            unrelated_skill = write_skill(repo / "skills" / "claude", "other", "# Original\n")
            cli.run_git(repo, "add", ".gitignore", "skills/claude/other/SKILL.md")
            cli.run_git(repo, "commit", "-m", "initial")
            (repo / "unrelated.txt").write_text("keep staged\n")
            cli.run_git(repo, "add", "unrelated.txt")
            (unrelated_skill / "SKILL.md").write_text("# Local edit\n")
            write_skill(base / "codex", "one", "# One\n")

            exit_code = run_cli([
                "backup", "--repo", str(repo), "--root", "codex",
                *root_args(base), "--apply", "--commit",
            ])

            self.assertEqual(exit_code, 0)
            committed = cli.run_git(repo, "show", "--name-only", "--format=").stdout.splitlines()
            self.assertNotIn("unrelated.txt", committed)
            self.assertNotIn("skills/claude/other/SKILL.md", committed)
            self.assertEqual(
                cli.run_git(repo, "diff", "--cached", "--name-only").stdout.strip(),
                "unrelated.txt",
            )
            self.assertEqual(
                cli.run_git(repo, "diff", "--name-only").stdout.strip(),
                "skills/claude/other/SKILL.md",
            )

    def test_backup_commit_treats_skill_names_as_literal_git_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "vault"
            cli.ensure_git_repo(repo)
            cli.run_git(repo, "config", "user.name", "Synchro Tests")
            cli.run_git(repo, "config", "user.email", "synchro@example.invalid")
            unrelated = repo / "skills" / "codex" / "other" / "note.txt"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text("original\n")
            cli.run_git(repo, "add", "skills/codex/other/note.txt")
            cli.run_git(repo, "commit", "-m", "initial")
            unrelated.write_text("unrelated secret\n")
            write_skill(base / "codex", "*", "# Literal star\n")

            exit_code = run_cli([
                "backup", "--repo", str(repo), "--root", "codex",
                *root_args(base), "--apply", "--commit",
            ])

            self.assertEqual(exit_code, 0)
            committed = cli.run_git(repo, "show", "--name-only", "--format=").stdout.splitlines()
            self.assertIn("skills/codex/*/SKILL.md", committed)
            self.assertNotIn("skills/codex/other/note.txt", committed)
            self.assertEqual(
                cli.run_git(repo, "diff", "--name-only").stdout.strip(),
                "skills/codex/other/note.txt",
            )

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

    def test_factory_rejects_plugin_skill_symlink_outside_plugin_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "vault"
            plugin_skills = base / "factory-plugins" / "plugins" / "core" / "skills"
            plugin_skills.mkdir(parents=True)
            external = write_skill(base, "private-plugin-skill", "# Private\n")
            (external / "credentials.json").write_text('{"token":"secret"}\n')
            (plugin_skills / "alias").symlink_to(external, target_is_directory=True)

            exit_code = run_cli([
                "backup", "--repo", str(repo), "--root", "factory",
                *root_args(base), "--apply",
            ])

            self.assertEqual(exit_code, 1)
            self.assertFalse((repo / "skills" / "factory" / "alias").exists())

    def test_factory_rejects_symlinked_plugin_outside_marketplace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "vault"
            plugins = base / "factory-plugins" / "plugins"
            plugins.mkdir(parents=True)
            external_plugin = base / "outside-plugin"
            external_skill = write_skill(external_plugin / "skills", "leak", "# Leak\n")
            (external_skill / "credentials.json").write_text('{"token":"secret"}\n')
            (plugins / "core").symlink_to(external_plugin, target_is_directory=True)

            exit_code = run_cli([
                "backup", "--repo", str(repo), "--root", "factory",
                *root_args(base), "--apply",
            ])

            self.assertEqual(exit_code, 1)
            self.assertFalse((repo / "skills" / "factory" / "leak").exists())

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

    def test_restore_rejects_symlinked_vault_tool_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "vault"
            (repo / "skills").mkdir(parents=True)
            external_root = base / "private-source"
            external_skill = write_skill(external_root, "leak", "# Leak\n")
            (external_skill / "credentials.json").write_text('{"token":"secret"}\n')
            (repo / "skills" / "codex").symlink_to(external_root, target_is_directory=True)

            exit_code = run_cli([
                "restore", "--repo", str(repo), "--from", "codex", "--to", "claude",
                *root_args(base), "--apply",
            ])

            self.assertEqual(exit_code, 1)
            self.assertFalse((base / "claude" / "leak").exists())

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

    def test_restore_requested_missing_skill_returns_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "vault"
            write_skill(repo / "skills" / "codex", "one", "# One\n")

            exit_code = run_cli([
                "restore", "--repo", str(repo), "--from", "codex", "--to", "claude",
                "--name", "absent", *root_args(base),
            ])

            self.assertEqual(exit_code, 1)

    def test_doctor_reports_configured_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_skill(base / "agy", "four", "# Four\n")

            exit_code = run_cli(["doctor", *root_args(base)])

            self.assertEqual(exit_code, 0)

    def test_audit_returns_drift_when_a_skill_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_skill(base / "codex", "one", "# One\n")
            (base / "claude").mkdir()

            exit_code = run_cli(["audit", *root_args(base)])

            self.assertEqual(exit_code, 2)

    def test_backup_excludes_local_and_secret_like_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            codex = base / "codex"
            claude = base / "claude"
            repo = base / "vault"
            skill = write_skill(codex, "one", "# One\n")
            (skill / ".envrc").write_text("SECRET=1\n")
            (skill / "settings.local.json").write_text("{}\n")
            (skill / "module.pyo").write_text("bytecode\n")
            (skill / ".pytest_cache").mkdir()
            (skill / ".pytest_cache" / "state").write_text("cache\n")

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
            self.assertFalse((backed_up / "module.pyo").exists())
            self.assertFalse((backed_up / ".pytest_cache").exists())


if __name__ == "__main__":
    unittest.main()
