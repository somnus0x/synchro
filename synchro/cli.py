from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Iterable


DEFAULT_TOOL_ROOTS = {
    # Codex and Antigravity both implement the agent-skills user root. Keeping
    # separate defaults makes one physical skill appear twice in Codex because
    # Codex also discovers ~/.agents/skills.
    "codex": "~/.agents/skills",
    "claude": "~/.claude/skills",
    "factory": "~/.factory/skills",
    "agy": "~/.agents/skills",
}

LEGACY_CODEX_ROOT = "~/.codex/skills"

# Runtime root map. Extended by ~/.synchro/config.json custom roots so a machine
# with skills in non-default locations (multiple skill repos, a monorepo dir) can
# register them as first-class named sources/targets. Defaults are never overwritten.
TOOL_ROOTS = dict(DEFAULT_TOOL_ROOTS)

TOOLS = tuple(TOOL_ROOTS)

DEFAULT_CONFIG_PATH = "~/.synchro/config.json"
LEGACY_CONFIG_PATH = "~/.skillmine/config.json"
MANIFEST_NAME = "synchro-manifest.json"
LEGACY_MANIFEST_NAME = "skillmine-manifest.json"

# Path-safe custom root name: must start with an alnum or underscore and use
# only alnum/dot/underscore/hyphen thereafter. This blocks path separators and
# ".."/"." traversal (a root name is a vault path component: skills/<name>/...)
# while still accepting the names old skillmine allowed (e.g. "my.skills",
# "_local") so migrated/legacy configs don't hard-fail every command.
CUSTOM_ROOT_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
RESERVED_ROOT_NAMES = {"all", "factory_plugins", "factory-plugins"}
BUILTIN_ROOT_NAMES_CASEFOLDED = {name.casefold() for name in DEFAULT_TOOL_ROOTS}
RESERVED_ROOT_NAMES_CASEFOLDED = {name.casefold() for name in RESERVED_ROOT_NAMES}


class SynchroError(RuntimeError):
    """A user-facing operational error that should not produce a traceback."""


def load_config_roots(config_path: str | None) -> dict[str, str]:
    """Read custom named roots from a Synchro config file.

    Config shape:
        {"roots": {"myskills": "/path/to/skills-repo", "lib": "/path/to/lib/skills"}}

    Returns an empty dict when the file is absent (config is optional). A custom
    root whose name collides with a built-in tool is refused, so config can only
    ADD roots, never silently rebind codex/claude/factory/agy.
    """
    path = expand_path(config_path or DEFAULT_CONFIG_PATH)
    if config_path is None and not path.exists():
        legacy_path = expand_path(LEGACY_CONFIG_PATH)
        if legacy_path.exists():
            path = legacy_path
    if not path.exists():
        if config_path is not None:
            raise SystemExit(f"config not found: {path}")
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"invalid config {path}: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"invalid config {path}: top level must be an object")
    roots = data.get("roots", {})
    if not isinstance(roots, dict):
        raise SystemExit(f"invalid config {path}: 'roots' must be an object")
    custom: dict[str, str] = {}
    custom_names_casefolded: set[str] = set()
    for name, value in roots.items():
        folded_name = name.casefold()
        if folded_name in BUILTIN_ROOT_NAMES_CASEFOLDED:
            raise SystemExit(
                f"invalid config {path}: '{name}' collides with a built-in tool; "
                "custom roots must use a distinct name"
            )
        if folded_name in RESERVED_ROOT_NAMES_CASEFOLDED:
            raise SystemExit(f"invalid config {path}: root name '{name}' is reserved")
        if folded_name in custom_names_casefolded:
            raise SystemExit(f"invalid config {path}: root name '{name}' collides by case")
        if not CUSTOM_ROOT_NAME.fullmatch(name):
            raise SystemExit(
                f"invalid config {path}: root name '{name}' must start with a "
                "letter, number, or underscore and contain only letters, "
                "numbers, dots, underscores, and hyphens"
            )
        if not isinstance(value, str) or not value.strip():
            raise SystemExit(f"invalid config {path}: root '{name}' must be a string path")
        custom_names_casefolded.add(folded_name)
        custom[name] = value
    return custom


def register_custom_roots(custom: dict[str, str]) -> None:
    """Merge custom roots into the runtime TOOL_ROOTS / TOOLS tables."""
    global TOOLS
    TOOL_ROOTS.clear()
    TOOL_ROOTS.update(DEFAULT_TOOL_ROOTS)
    TOOL_ROOTS.update(custom)
    TOOLS = tuple(TOOL_ROOTS)

PROTECTED_EXCLUDES = {
    ".git",
    ".aws",
    ".gcloud",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".ssh",
    ".venv",
    "*.jks",
    "*.key",
    "*.keystore",
    "*.p12",
    "*.pem",
    "*.pfx",
    "*service-account*.json",
    "*service_account*.json",
    ".env*",
    "client_secret*.json",
    "credentials.json",
    "credentials.*.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "secret.*",
    "secrets.*",
    "settings.local.json",
    "terraform.tfstate*",
    "token.json",
    "tokens.json",
}

DISPOSABLE_EXCLUDES = {
    ".DS_Store",
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".synchro-*",
}

EXCLUDES = PROTECTED_EXCLUDES | DISPOSABLE_EXCLUDES


@dataclass(frozen=True)
class Skill:
    tool: str
    name: str
    path: Path
    digest: str
    managed: bool = False


def expand_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def matches_patterns(name: str, patterns: Iterable[str]) -> bool:
    # fnmatch applies os.path.normcase, which is identity on POSIX — so plain
    # fnmatch is case-sensitive on macOS/Linux and ".ENV" would slip past
    # ".env*". Lowercase both sides so secret-file exclusion holds for case
    # variants (critical on case-insensitive filesystems like APFS).
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(lowered, pattern.lower()) for pattern in patterns)


def is_excluded(name: str) -> bool:
    return matches_patterns(name, EXCLUDES)


def is_protected(name: str) -> bool:
    return matches_patterns(name, PROTECTED_EXCLUDES)


def iter_skill_files(skill_dir: Path) -> Iterable[Path]:
    root = skill_dir.resolve()
    stack = [root]
    while stack:
        current = stack.pop()
        for child in sorted(current.iterdir(), key=lambda p: p.name):
            if is_excluded(child.name):
                continue
            if child.is_symlink():
                yield child
                continue
            if child.is_dir():
                stack.append(child)
                continue
            if child.is_file():
                yield child


def hash_skill(skill_dir: Path) -> str:
    root = skill_dir.resolve()
    digest = hashlib.sha256()
    for file_path in iter_skill_files(root):
        relative = file_path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if file_path.is_symlink():
            digest.update(b"symlink\0")
            digest.update(os.fsencode(os.readlink(file_path)))
        else:
            digest.update(b"file\0")
            with file_path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def make_skill(tool: str, name: str, path: Path, managed: bool = False) -> Skill:
    return Skill(
        tool=tool,
        name=name,
        path=path,
        digest=hash_skill(path),
        managed=managed,
    )


def discover_flat_skills(tool: str, root: Path, managed: bool = False) -> dict[str, Skill]:
    if not root.is_dir():
        return {}

    skills: dict[str, Skill] = {}
    resolved_root = root.resolve()
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if is_excluded(entry.name):
            continue
        if entry.is_symlink():
            try:
                resolved_entry = entry.resolve(strict=True)
            except (OSError, RuntimeError):
                # Broken symlink, or a loop (ELOOP raises OSError on 3.11+, the
                # package's own floor — the old RuntimeError catch never fired).
                # Skip it rather than crash every command with a raw traceback.
                continue
            if not resolved_entry.is_dir() or not (resolved_entry / "SKILL.md").exists():
                continue
            if not is_within(resolved_entry, resolved_root):
                # A skill symlinked in from outside its root (common dotfiles
                # setup) is skipped with a warning, not fatal — one such entry
                # must not abort read-only audit/doctor or drop every other
                # skill from a backup.
                print(
                    f"warning: skipping skill symlink outside root: {entry} -> {resolved_entry}",
                    file=sys.stderr,
                )
                continue
        elif not entry.is_dir() or not (entry / "SKILL.md").exists():
            continue
        skills[entry.name] = make_skill(tool, entry.name, entry, managed=managed)
    return skills


def discover_factory_plugin_skills(plugin_root: Path) -> dict[str, Skill]:
    resolved_plugin_root = plugin_root.resolve()
    if (plugin_root / "plugins").is_dir():
        marketplace_roots = [plugin_root]
    elif plugin_root.is_dir():
        marketplace_roots = [entry for entry in sorted(plugin_root.iterdir(), key=lambda p: p.name) if (entry / "plugins").is_dir()]
    else:
        marketplace_roots = []

    if not marketplace_roots:
        return {}

    skills: dict[str, Skill] = {}
    for marketplace_root in marketplace_roots:
        resolved_marketplace_root = marketplace_root.resolve()
        if not is_within(resolved_marketplace_root, resolved_plugin_root):
            raise SynchroError(
                f"refusing marketplace outside plugin root: {marketplace_root} -> {resolved_marketplace_root}"
            )
        plugins_root = marketplace_root / "plugins"
        resolved_plugins_root = plugins_root.resolve()
        if not is_within(resolved_plugins_root, resolved_marketplace_root):
            raise SynchroError(
                f"refusing plugins directory outside marketplace: {plugins_root} -> {resolved_plugins_root}"
            )
        for plugin_dir in sorted(plugins_root.iterdir(), key=lambda p: p.name):
            resolved_plugin_dir = plugin_dir.resolve()
            if not is_within(resolved_plugin_dir, resolved_plugins_root):
                raise SynchroError(
                    f"refusing plugin outside marketplace: {plugin_dir} -> {resolved_plugin_dir}"
                )
            skills_dir = plugin_dir / "skills"
            if not skills_dir.is_dir():
                continue
            resolved_skills_dir = skills_dir.resolve()
            if not is_within(resolved_skills_dir, resolved_plugin_dir):
                raise SynchroError(
                    f"refusing skills directory outside plugin: {skills_dir} -> {resolved_skills_dir}"
                )
            for entry in sorted(skills_dir.iterdir(), key=lambda p: p.name):
                if is_excluded(entry.name):
                    continue
                if entry.is_symlink():
                    try:
                        resolved_entry = entry.resolve(strict=True)
                    except (OSError, RuntimeError):
                        continue
                    if not resolved_entry.is_dir() or not (resolved_entry / "SKILL.md").exists():
                        continue
                    if not is_within(resolved_entry, resolved_skills_dir):
                        print(
                            f"warning: skipping plugin skill symlink outside root: {entry} -> {resolved_entry}",
                            file=sys.stderr,
                        )
                        continue
                elif not entry.is_dir() or not (entry / "SKILL.md").exists():
                    continue
                skills.setdefault(entry.name, make_skill("factory", entry.name, entry, managed=True))
    return skills


def discover_skills(tool: str, roots: dict[str, Path]) -> dict[str, Skill]:
    skills = discover_flat_skills(tool, roots[tool])
    if tool == "factory":
        for name, skill in discover_factory_plugin_skills(roots["factory_plugins"]).items():
            skills.setdefault(name, skill)
    return skills


def roots_from_args(args: argparse.Namespace) -> dict[str, Path]:
    roots = {tool: expand_path(getattr(args, f"{tool}_root")) for tool in TOOLS}
    roots["factory_plugins"] = expand_path(args.factory_plugins_root)
    return roots


def roots_are_shared(left: Path, right: Path) -> bool:
    """True when two tool names resolve to the same physical skill root."""
    if left == right:
        return True
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def unique_tools_by_root(
    tools: Iterable[str],
    roots: dict[str, Path],
) -> tuple[list[str], dict[str, str]]:
    """Collapse tool aliases that point at one install root.

    The first tool remains the canonical label for aggregate operations. An
    explicitly selected single tool is therefore preserved, while `all` avoids
    auditing or backing up the same install twice.
    """
    unique: list[str] = []
    aliases: dict[str, str] = {}
    for tool in tools:
        owner = next(
            (
                candidate
                for candidate in unique
                if tool != "factory"
                and candidate != "factory"
                and roots_are_shared(roots[tool], roots[candidate])
            ),
            None,
        )
        if owner is None:
            unique.append(tool)
        else:
            aliases[tool] = owner
    return unique, aliases


def tool_root_exists(tool: str, roots: dict[str, Path]) -> bool:
    if roots[tool].is_dir():
        return True
    return tool == "factory" and roots["factory_plugins"].is_dir()


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def assert_copy_paths_do_not_overlap(src: Path, dest: Path) -> None:
    resolved_src = src.resolve()
    resolved_dest = dest.resolve()
    lexical_overlap = is_within(resolved_src, resolved_dest) or is_within(
        resolved_dest,
        resolved_src,
    )

    def same_file(left: Path, right: Path) -> bool:
        try:
            return os.path.samefile(left, right)
        except OSError:
            return False

    physical_overlap = any(same_file(resolved_src, candidate) for candidate in (resolved_dest, *resolved_dest.parents))
    physical_overlap = physical_overlap or any(
        same_file(resolved_dest, candidate) for candidate in (resolved_src, *resolved_src.parents)
    )
    if lexical_overlap or physical_overlap:
        raise SynchroError(f"refusing overlapping copy: {resolved_src} -> {resolved_dest}")


def validated_vault_root(repo: Path) -> tuple[Path, Path]:
    repo_root = repo.resolve()
    vault_path = repo_root / "skills"
    if vault_path.is_symlink():
        raise SynchroError(f"refusing symlinked vault root: {vault_path}")
    vault_root = vault_path.resolve()
    if not is_within(vault_root, repo_root):
        raise SynchroError(f"refusing vault root outside repo: {vault_root}")
    return vault_path, vault_root


def vault_destination(repo: Path, tool: str, name: str) -> tuple[Path, Path]:
    repo_dest = Path("skills") / tool / name
    vault_path, vault_root = validated_vault_root(repo)
    tool_path = vault_path / tool
    dest_path = tool_path / name
    for candidate in (tool_path, dest_path):
        if candidate.is_symlink():
            raise SynchroError(f"refusing symlinked vault destination: {candidate}")
    resolved_dest = dest_path.resolve()
    if not is_within(resolved_dest, vault_root):
        raise SynchroError(f"refusing backup destination outside vault: {resolved_dest}")
    return repo_dest, dest_path


def backup_path(base: Path, tool: str, name: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return base / timestamp / tool / name


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


def copy_skill(src: Path, dest: Path) -> None:
    assert_copy_paths_do_not_overlap(src, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f".synchro-{dest.name}-", dir=dest.parent))
    try:
        shutil.copytree(
            src.resolve(),
            staged,
            symlinks=True,
            ignore=copy_ignore,
            dirs_exist_ok=True,
        )
        if dest.exists() or dest.is_symlink():
            remove_path(dest)
        staged.replace(dest)
    finally:
        if staged.exists() or staged.is_symlink():
            remove_path(staged)


def backup_existing_path(src: Path, dest: Path) -> None:
    """Preserve a displaced target, excluding entries matched by EXCLUDES.

    Sync/restore must refuse targets containing PROTECTED_EXCLUDES before calling
    this helper. The supported replacement path can therefore omit disposable
    caches without either duplicating secrets or deleting protected local data.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        dest.symlink_to(os.readlink(src), target_is_directory=src.is_dir())
    elif src.is_dir():
        shutil.copytree(src, dest, symlinks=True, ignore=copy_ignore)
    elif not is_excluded(src.name):
        shutil.copy2(src, dest, follow_symlinks=False)


def copy_ignore(_dir: str, names: list[str]) -> set[str]:
    return {name for name in names if is_excluded(name)}


def find_matching_entries(skill_dir: Path, patterns: Iterable[str]) -> list[Path]:
    """List matching entries without reading their contents or following links."""
    if skill_dir.is_symlink() or not skill_dir.is_dir():
        return []
    root = skill_dir
    excluded: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        for child in current.iterdir():
            if matches_patterns(child.name, patterns):
                excluded.append(child.relative_to(root))
            elif child.is_dir() and not child.is_symlink():
                stack.append(child)
    return sorted(excluded)


def find_excluded_entries(skill_dir: Path) -> list[Path]:
    return find_matching_entries(skill_dir, EXCLUDES)


def find_protected_entries(skill_dir: Path) -> list[Path]:
    return find_matching_entries(skill_dir, PROTECTED_EXCLUDES)


def run_git(
    repo: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def ensure_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        subprocess.run(
            ["git", "init", str(repo)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


def git_tracks(repo: Path, name: str) -> bool:
    """True if `name` is tracked in the vault index (even if deleted from the
    worktree). Lets the caller stage a removed legacy manifest's deletion without
    a fatal unmatched-pathspec `git add` when the file was never tracked."""
    result = run_git(repo, "ls-files", "--", name, check=False)
    return result.returncode == 0 and bool(result.stdout.strip())


def commit_paths(repo: Path, paths: list[str], message: str) -> bool:
    """Commit only managed paths without consuming unrelated index entries."""
    literal_environment = os.environ.copy()
    literal_environment["GIT_LITERAL_PATHSPECS"] = "1"
    # Staging into the REAL index happens only after a successful commit (below).
    # The old code added here first, which clobbered content the user hand-staged
    # under a managed path even when there was ultimately nothing to commit.

    descriptor, index_name = tempfile.mkstemp(prefix="synchro-index-")
    os.close(descriptor)
    Path(index_name).unlink()
    temporary_index = Path(index_name)
    environment = literal_environment.copy()
    environment["GIT_INDEX_FILE"] = str(temporary_index)
    try:
        head = run_git(repo, "rev-parse", "--verify", "HEAD", check=False)
        if head.returncode == 0:
            run_git(repo, "read-tree", "HEAD", env=environment)
        else:
            run_git(repo, "read-tree", "--empty", env=environment)
        run_git(repo, "add", "--all", "--", *paths, env=environment)
        diff = run_git(
            repo,
            "diff",
            "--cached",
            "--quiet",
            "--",
            *paths,
            check=False,
            env=environment,
        )
        if diff.returncode == 0:
            return False
        if diff.returncode != 1:
            raise SynchroError(f"git diff failed: {diff.stderr.strip()}")
        run_git(repo, "commit", "-m", message, env=environment)
        # Sync the real index for just the committed paths so `git status` is
        # clean afterward (no phantom staged-deletions vs the new HEAD). Reached
        # only when we actually committed, so it never touches a path — or a
        # user's staging of one — that this backup left alone.
        run_git(repo, "add", "--all", "--", *paths, env=literal_environment)
        return True
    finally:
        if temporary_index.exists():
            temporary_index.unlink()


def cmd_audit(args: argparse.Namespace) -> int:
    roots = roots_from_args(args)
    active_tools = selected_tools(args)
    unique_tools, aliases = unique_tools_by_root(active_tools, roots)
    discovered = {tool: discover_skills(tool, roots) for tool in unique_tools}
    all_skills = {
        tool: discovered[aliases.get(tool, tool)]
        for tool in active_tools
    }

    for tool in active_tools:
        root = roots[tool]
        skills = all_skills[tool]
        status = "missing root" if not root.exists() else f"{len(skills)} skills"
        if tool == "factory" and roots["factory_plugins"].exists():
            plugin_count = sum(1 for skill in skills.values() if skill.managed)
            status = f"{status}, {plugin_count} plugin skills"
        print(f"{tool}: {root} ({status})")

    comparable_candidates = [
        tool for tool in active_tools if roots[tool].exists() or all_skills[tool]
    ]
    comparable_tools = [tool for tool in comparable_candidates if tool not in aliases]
    for alias, owner in aliases.items():
        print(f"shared root: {alias} -> {roots[alias]} (same as {owner})")

    if len(comparable_tools) < 2:
        count = sum(len(all_skills[tool]) for tool in comparable_tools)
        print(f"summary: roots={len(comparable_tools)} skills={count} drift=0")
        return 0

    names = sorted({name for tool in comparable_tools for name in all_skills[tool]})

    same = 0
    drift = 0
    missing: dict[str, int] = {tool: 0 for tool in comparable_tools}

    for name in names:
        present = {tool: all_skills[tool][name] for tool in comparable_tools if name in all_skills[tool]}
        absent = [tool for tool in comparable_tools if tool not in present]
        digest_count = len({skill.digest for skill in present.values()})
        if len(present) == len(comparable_tools) and digest_count == 1:
            same += 1
            if args.verbose:
                print(f"same: {name}")
        elif digest_count > 1:
            drift += 1
            tools = ", ".join(sorted(present))
            print(f"drift: {name} ({tools})")
        for tool in absent:
            missing[tool] += 1
            print(f"missing in {tool}: {name}")

    missing_summary = " ".join(f"missing_{tool}={count}" for tool, count in missing.items())
    print(
        "summary: "
        f"same={same} drift={drift} {missing_summary}"
    )
    return 0 if drift == 0 and sum(missing.values()) == 0 else 2


def selected_tools(args: argparse.Namespace) -> list[str]:
    if args.root == "all":
        return list(TOOLS)
    return [args.root]


def normalize_manifest_dest(dest: object, repo: Path) -> str | None:
    if not isinstance(dest, str) or not dest:
        return None

    path = Path(dest)
    def skills_suffix(parts: tuple[str, ...]) -> str | None:
        for index, part in enumerate(parts):
            if part.lower() == "skills":
                return Path(*parts[index:]).as_posix()
        return None

    # Only treat a dest as Windows when it has NO forward slash: this module
    # always writes dests via as_posix() (forward slashes), so a value with both
    # separators is a POSIX path holding a literal backslash in a component, not
    # a Windows path — rewriting it would break dest-keyed manifest merging.
    if "\\" in dest and "/" not in dest:
        windows_path = PureWindowsPath(dest)
        suffix = skills_suffix(windows_path.parts)
        if suffix:
            return suffix
        if not windows_path.is_absolute():
            return Path(*windows_path.parts).as_posix()

    if not path.is_absolute():
        return path.as_posix()

    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        pass

    suffix = skills_suffix(path.parts)
    if suffix:
        return suffix

    return dest


def load_manifest_file(manifest_path: Path, repo: Path) -> dict[str, object]:
    """Read and normalize one manifest, or return an empty skeleton."""
    if not manifest_path.exists():
        return {"roots": {}, "skills": []}
    try:
        raw = manifest_path.read_text()
    except OSError as exc:
        # A transient read error must NOT silently return an empty index — that
        # drops every entry other roots/machines recorded (the clobber
        # regression fixed in 8a37734, resurfacing through the error path).
        raise SynchroError(f"cannot read manifest {manifest_path}: {exc}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # A corrupt (unparseable) index shouldn't block a backup — start fresh.
        return {"roots": {}, "skills": []}
    if not isinstance(data, dict):
        return {"roots": {}, "skills": []}
    roots = data.get("roots", {})
    skills = data.get("skills", [])
    if not isinstance(roots, dict) or not isinstance(skills, list):
        return {"roots": {}, "skills": []}

    normalized_skills = []
    for entry in skills:
        if not isinstance(entry, dict):
            continue
        normalized = dict(entry)
        normalized_dest = normalize_manifest_dest(normalized.get("dest"), repo)
        if not normalized_dest:
            continue
        normalized["dest"] = normalized_dest
        normalized_skills.append(normalized)
    return {
        "roots": {name: value for name, value in roots.items() if isinstance(value, str)},
        "skills": normalized_skills,
    }


def legacy_manifest_can_migrate(manifest_path: Path, repo: Path) -> bool:
    """Only remove a legacy manifest when every indexed record is recoverable."""
    try:
        data = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    roots = data.get("roots", {})
    skills = data.get("skills", [])
    if not isinstance(roots, dict) or not isinstance(skills, list):
        return False
    if not all(isinstance(value, str) for value in roots.values()):
        return False
    return all(
        isinstance(entry, dict) and normalize_manifest_dest(entry.get("dest"), repo)
        for entry in skills
    )


def load_existing_manifest(repo: Path) -> dict[str, object]:
    """Read current and legacy manifests, merging current records last.

    A backup vault can be written by more than one machine. The manifest is the
    repo's index of what it holds, so a partial backup (`--root myskills`) must
    MERGE into it, not overwrite the entries other machines/roots wrote. Returns
    the parsed manifest with `roots`/`skills` present; on any read/parse error it
    starts fresh (a corrupt index shouldn't block a backup).
    A legacy Skillmine manifest is read once and removed after the next
    successful backup writes the Synchro manifest.
    """
    roots: dict[str, str] = {}
    by_dest: dict[str, dict] = {}
    for name in (LEGACY_MANIFEST_NAME, MANIFEST_NAME):
        manifest = load_manifest_file(repo / name, repo)
        manifest_roots = manifest.get("roots", {})
        if isinstance(manifest_roots, dict):
            roots.update(manifest_roots)
        manifest_skills = manifest.get("skills", [])
        if isinstance(manifest_skills, list):
            for entry in manifest_skills:
                if isinstance(entry, dict) and isinstance(entry.get("dest"), str):
                    by_dest[entry["dest"]] = entry
    return {"roots": roots, "skills": list(by_dest.values())}


def merge_manifest(existing: dict[str, object], new_roots: dict[str, str], new_skills: list[dict]) -> dict[str, object]:
    """Merge this run's roots+skills over an existing manifest, keyed by `dest`.

    `dest` is the repo-relative identity of a backed-up skill, so same-dest
    entries from this run replace their prior record while every other entry
    (other roots, other machines) is preserved. Roots union the same way.
    Does NOT prune skills whose source vanished — that's an explicit --prune
    concern, out of scope here.
    """
    existing_skills = existing.get("skills", [])
    if not isinstance(existing_skills, list):
        existing_skills = []
    by_dest: dict[str, dict] = {
        entry["dest"]: entry
        for entry in existing_skills
        if isinstance(entry, dict) and isinstance(entry.get("dest"), str)
    }
    for entry in new_skills:
        by_dest[entry["dest"]] = entry
    existing_roots = existing.get("roots", {})
    merged_roots = dict(existing_roots) if isinstance(existing_roots, dict) else {}
    merged_roots.update(new_roots)
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "roots": merged_roots,
        "skills": sorted(by_dest.values(), key=lambda e: e["dest"]),
    }


def write_json_atomic(path: Path, data: dict[str, object]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def cmd_backup(args: argparse.Namespace) -> int:
    roots = roots_from_args(args)
    repo = expand_path(args.repo)
    tools = selected_tools(args)
    if args.root != "all" and not tool_root_exists(args.root, roots):
        print(f"missing root: {args.root} -> {roots[args.root]}", file=sys.stderr)
        return 1

    tools, aliases = unique_tools_by_root(tools, roots)
    for alias, owner in aliases.items():
        print(f"skip shared root: {alias} -> {roots[alias]} (backed up as {owner})")

    new_roots = {tool: str(roots[tool]) for tool in tools}
    new_skills: list[dict] = []

    planned: list[tuple[Skill, Path]] = []
    for tool in tools:
        if not roots[tool].exists() and tool != "factory":
            print(f"skip missing root: {tool} -> {roots[tool]}")
            continue
        if tool == "factory" and not roots[tool].exists() and not roots["factory_plugins"].exists():
            print(f"skip missing root: {tool} -> {roots[tool]}")
            continue
        for skill in discover_skills(tool, roots).values():
            repo_dest, dest = vault_destination(repo, tool, skill.name)
            assert_copy_paths_do_not_overlap(skill.path, dest)
            planned.append((skill, dest))
            new_skills.append(
                {
                    "tool": tool,
                    "name": skill.name,
                    "source": str(skill.path),
                    "dest": repo_dest.as_posix(),
                    "sha256": skill.digest,
                }
            )

    for skill, dest in planned:
        print(f"{'copy' if args.apply else 'would copy'}: {skill.tool}/{skill.name} -> {dest}")

    if not args.apply:
        print("dry-run: pass --apply to write backup repo")
        return 0

    legacy_manifest_path = repo / LEGACY_MANIFEST_NAME
    legacy_manifest_present = legacy_manifest_path.is_file() or legacy_manifest_path.is_symlink()
    migrate_legacy_manifest = legacy_manifest_present and legacy_manifest_can_migrate(
        legacy_manifest_path,
        repo,
    )
    existing_manifest = load_existing_manifest(repo)
    ensure_git_repo(repo)
    for skill, dest in planned:
        dest.parent.mkdir(parents=True, exist_ok=True)
        copy_skill(skill.path, dest)

    manifest = merge_manifest(existing_manifest, new_roots, new_skills)
    manifest_path = repo / MANIFEST_NAME
    write_json_atomic(manifest_path, manifest)
    if migrate_legacy_manifest:
        legacy_manifest_path.unlink()

    if args.commit:
        managed_paths = sorted(entry["dest"] for entry in new_skills)
        managed_paths.append(MANIFEST_NAME)
        # Stage the legacy manifest's removal only when git actually tracks it.
        # Untracked (fresh vault, or prior --apply-without-commit runs) → adding
        # it makes `git add -- skillmine-manifest.json` fatal on an unmatched
        # pathspec. Tracked-but-already-deleted (migrate flag consumed in an
        # earlier run) → its deletion still needs committing, which the old
        # `if migrate_legacy_manifest` gate skipped.
        if git_tracks(repo, LEGACY_MANIFEST_NAME):
            managed_paths.append(LEGACY_MANIFEST_NAME)
        if not commit_paths(repo, managed_paths, args.message):
            print("git: no backup changes to commit")
        else:
            print(f"git: committed backup in {repo}")

    return 0


def discover_vault_skills(repo: Path, tool: str) -> dict[str, Skill]:
    vault_path, vault_root = validated_vault_root(repo)
    tool_path = vault_path / tool
    if tool_path.is_symlink():
        raise SynchroError(f"refusing symlinked vault tool root: {tool_path}")
    resolved_tool_path = tool_path.resolve()
    if not is_within(resolved_tool_path, vault_root):
        raise SynchroError(f"refusing vault tool root outside vault: {resolved_tool_path}")
    return discover_flat_skills(tool, tool_path)


def cmd_restore(args: argparse.Namespace) -> int:
    roots = roots_from_args(args)
    repo = expand_path(args.repo)
    source_skills = discover_vault_skills(repo, args.source)
    target_skills = discover_skills(args.target, roots)
    selected_names = set(args.name or source_skills.keys())
    backup_base = expand_path(args.backup_dir)
    actions = 0
    conflicts = 0
    missing = 0

    if not source_skills:
        print(f"missing backup source: {repo / 'skills' / args.source}")
        return 1

    for name in sorted(selected_names):
        source = source_skills.get(name)
        if not source:
            missing += 1
            print(f"missing backup skill: {args.source}/{name}")
            continue

        target = target_skills.get(name)
        target_path = roots[args.target] / name
        target_occupied = target_path.exists() or target_path.is_symlink()
        if target and target.digest == source.digest:
            print(f"same: {name}")
            continue

        if target is None and target_occupied and not args.force:
            conflicts += 1
            print(
                f"conflict: {target_path} exists but is not a skill; "
                "pass --force to replace it after backup"
            )
            continue

        if target and target.digest != source.digest and not args.force:
            conflicts += 1
            print(f"conflict: {name} exists in {args.target}; pass --force to replace with backup")
            continue

        if target and target.managed and args.force:
            conflicts += 1
            print(
                f"conflict: {args.target}/{name} is plugin-managed at {target.path}; "
                "refusing to replace it from backup"
            )
            continue

        protected = find_protected_entries(target_path) if target_occupied else []
        if protected:
            conflicts += 1
            preview = ", ".join(path.as_posix() for path in protected[:3])
            suffix = " ..." if len(protected) > 3 else ""
            print(f"blocked protected files: {args.target}/{name} ({preview}{suffix})")
            continue

        verb = "replace" if target_occupied else "restore"
        print(f"{verb if args.apply else 'would ' + verb}: {repo}/skills/{args.source}/{name} -> {target_path}")
        actions += 1

        if not args.apply:
            continue

        roots[args.target].mkdir(parents=True, exist_ok=True)
        if target_path.exists() or target_path.is_symlink():
            backup_dest = backup_path(backup_base, args.target, name)
            backup_existing_path(target_path, backup_dest)
            print(f"backup: {target_path} -> {backup_dest}")
        copy_skill(source.path, target_path)

    if not args.apply:
        print("dry-run: pass --apply to restore target skills")

    if conflicts:
        print(f"summary: actions={actions} conflicts={conflicts} missing={missing}")
        return 2

    if missing:
        print(f"summary: actions={actions} conflicts=0 missing={missing}")
        return 1

    print(f"summary: actions={actions} conflicts=0")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    if args.source == args.target:
        print("--from and --to must be different tools", file=sys.stderr)
        return 1

    roots = roots_from_args(args)
    if not tool_root_exists(args.source, roots):
        print(f"missing source root: {args.source} -> {roots[args.source]}", file=sys.stderr)
        return 1
    if (
        args.source != "factory"
        and args.target != "factory"
        and roots_are_shared(roots[args.source], roots[args.target])
    ):
        print(
            f"shared root: {args.source} and {args.target} -> {roots[args.source]}; "
            "nothing to copy"
        )
        return 0
    source_skills = discover_skills(args.source, roots)
    target_skills = discover_skills(args.target, roots)
    selected_names = set(args.name or source_skills.keys())
    backup_base = expand_path(args.backup_dir)
    actions = 0
    conflicts = 0
    missing = 0

    for name in sorted(selected_names):
        source = source_skills.get(name)
        if not source:
            missing += 1
            print(f"missing source: {args.source}/{name}")
            continue

        target = target_skills.get(name)
        target_path = roots[args.target] / name
        target_occupied = target_path.exists() or target_path.is_symlink()
        if target and target.digest == source.digest:
            print(f"same: {name}")
            continue

        if target is None and target_occupied and not args.force:
            conflicts += 1
            print(
                f"conflict: {target_path} exists but is not a skill; "
                "pass --force to replace it after backup"
            )
            continue

        if target and target.digest != source.digest and not args.force:
            conflicts += 1
            print(f"conflict: {name} exists in {args.target}; pass --force to replace with backup")
            continue

        if target and target.managed and args.force:
            conflicts += 1
            print(
                f"conflict: {args.target}/{name} is plugin-managed at {target.path}; "
                "refusing to replace it with a personal skill"
            )
            continue

        protected = find_protected_entries(target_path) if target_occupied else []
        if protected:
            conflicts += 1
            preview = ", ".join(path.as_posix() for path in protected[:3])
            suffix = " ..." if len(protected) > 3 else ""
            print(f"blocked protected files: {args.target}/{name} ({preview}{suffix})")
            continue

        verb = "replace" if target_occupied else "copy"
        print(f"{verb if args.apply else 'would ' + verb}: {args.source}/{name} -> {target_path}")
        actions += 1

        if not args.apply:
            continue

        roots[args.target].mkdir(parents=True, exist_ok=True)
        if target_path.exists() or target_path.is_symlink():
            backup_dest = backup_path(backup_base, args.target, name)
            backup_existing_path(target_path, backup_dest)
            print(f"backup: {target_path} -> {backup_dest}")
        copy_skill(source.path, target_path)

    if not args.apply:
        print("dry-run: pass --apply to write target skills")

    if conflicts:
        print(f"summary: actions={actions} conflicts={conflicts} missing={missing}")
        return 2

    if missing:
        print(f"summary: actions={actions} conflicts=0 missing={missing}")
        return 1

    print(f"summary: actions={actions} conflicts=0")
    return 0


def cmd_migrate_codex(args: argparse.Namespace) -> int:
    """Consolidate legacy Codex user skills into the shared agent-skills root."""
    legacy_root = expand_path(args.legacy_root)
    shared_root = expand_path(args.shared_root)
    backup_base = expand_path(args.backup_dir)

    if roots_are_shared(legacy_root, shared_root):
        print("legacy and shared roots must be different", file=sys.stderr)
        return 1
    if not legacy_root.is_dir():
        print(f"legacy root missing: {legacy_root}; nothing to migrate")
        return 0

    legacy_skills = discover_flat_skills("codex-legacy", legacy_root)
    shared_skills = discover_flat_skills("codex", shared_root)
    selected_names = set(args.name or legacy_skills.keys())
    plans: list[tuple[Skill, Path, bool]] = []
    moved = 0
    duplicates = 0
    conflicts = 0
    missing = 0

    for name in sorted(selected_names):
        source = legacy_skills.get(name)
        if source is None:
            missing += 1
            print(f"missing legacy skill: {name}")
            continue

        excluded = find_excluded_entries(source.path)
        if excluded:
            conflicts += 1
            preview = ", ".join(path.as_posix() for path in excluded[:3])
            suffix = " ..." if len(excluded) > 3 else ""
            print(f"blocked local-only files: {name} ({preview}{suffix})")
            continue

        target = shared_skills.get(name)
        target_path = shared_root / name
        if target is not None and target.digest != source.digest:
            conflicts += 1
            print(f"conflict: {name} differs between legacy and shared roots")
            continue
        if target is None and (target_path.exists() or target_path.is_symlink()):
            conflicts += 1
            print(f"conflict: non-skill target blocks migration: {target_path}")
            continue

        duplicate = target is not None
        plans.append((source, target_path, duplicate))
        verb = "remove duplicate" if duplicate else "move"
        print(
            f"{verb if args.apply else 'would ' + verb}: "
            f"{source.path} -> {target_path}"
        )

    if not args.apply:
        moved = sum(1 for _, _, duplicate in plans if not duplicate)
        duplicates = sum(1 for _, _, duplicate in plans if duplicate)
        print("dry-run: pass --apply to consolidate Codex skills")
        print(
            f"summary: moved={moved} duplicates_removed={duplicates} "
            f"conflicts={conflicts} missing={missing}"
        )
        if conflicts:
            return 2
        if missing:
            return 1
        return 0

    if conflicts or missing:
        print("preflight failed: no snapshot or live changes were made")
        print(
            f"summary: moved=0 duplicates_removed=0 "
            f"conflicts={conflicts} missing={missing}"
        )
        return 2 if conflicts else 1

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    snapshot_root = backup_base / timestamp / "pre-codex-migration"
    snapshot_skills = snapshot_root / "legacy"
    snapshot_sources = sorted(
        (source for source, _, _ in plans),
        key=lambda item: item.name,
    )
    for source in snapshot_sources:
        snapshot_path = snapshot_skills / source.name
        copy_skill(source.path, snapshot_path)
        if hash_skill(snapshot_path) != source.digest:
            raise SynchroError(
                f"snapshot verification failed for {source.name}; no live changes were made"
            )
    write_json_atomic(
        snapshot_root / "snapshot.json",
        {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "legacy_root": str(legacy_root),
            "shared_root": str(shared_root),
            "skills": [
                {"name": skill.name, "sha256": skill.digest}
                for skill in snapshot_sources
            ],
        },
    )
    print(f"snapshot: {legacy_root} -> {snapshot_root}")

    for source, target_path, duplicate in plans:
        if not duplicate:
            shared_root.mkdir(parents=True, exist_ok=True)
            copy_skill(source.path, target_path)
            if hash_skill(target_path) != source.digest:
                raise SynchroError(
                    f"migration verification failed for {source.name}; "
                    f"restore from {snapshot_root}"
                )
            moved += 1
        else:
            duplicates += 1
        remove_path(source.path)

    print(
        f"summary: moved={moved} duplicates_removed={duplicates} "
        "conflicts=0 missing=0"
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    roots = roots_from_args(args)
    print(f"synchro: {Path(__file__).resolve().parents[1]}")
    git = shutil.which("git")
    droid = shutil.which("droid")
    print(f"git: {git or 'missing'}")
    print(f"droid: {droid or 'missing'}")

    unique_tools, aliases = unique_tools_by_root(TOOLS, roots)
    discovered = {tool: discover_skills(tool, roots) for tool in unique_tools}
    for tool in TOOLS:
        skills = discovered[aliases.get(tool, tool)]
        root = roots[tool]
        status = "exists" if root.exists() else "missing"
        detail = f"{len(skills)} skills"
        if tool == "factory" and roots["factory_plugins"].exists():
            plugin_count = sum(1 for skill in skills.values() if skill.managed)
            detail = f"{detail}, {plugin_count} plugin-managed"
        print(f"{tool}: {root} ({status}, {detail})")
        if tool in aliases:
            print(f"  shared with: {aliases[tool]}")

    if roots["factory_plugins"].exists():
        print(f"factory plugins: {roots['factory_plugins']} (exists)")
    else:
        print(f"factory plugins: {roots['factory_plugins']} (missing)")

    return 0


def add_root_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        default=None,
        help=f"Synchro config file with custom named roots (default: {DEFAULT_CONFIG_PATH})",
    )
    for tool, default in TOOL_ROOTS.items():
        parser.add_argument(f"--{tool}-root", dest=f"{tool}_root", default=default)
    parser.add_argument(
        "--factory-plugins-root",
        default="~/.factory/plugins/marketplaces",
        help="Factory/Droid plugin marketplaces root, or one marketplace root, to read installed plugin skills from",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="synchro")
    subcommands = parser.add_subparsers(dest="command", required=True)

    audit = subcommands.add_parser("audit", help="compare configured skill installs")
    add_root_args(audit)
    audit.add_argument("--root", choices=["all", *TOOLS], default="all")
    audit.add_argument("--verbose", action="store_true")
    audit.set_defaults(func=cmd_audit)

    backup = subcommands.add_parser("backup", help="back up skills into a git repo")
    add_root_args(backup)
    backup.add_argument("--repo", required=True, help="git-backed backup repo path")
    backup.add_argument("--root", choices=["all", *TOOLS], default="all")
    backup.add_argument("--apply", action="store_true")
    backup.add_argument("--commit", action="store_true")
    backup.add_argument("-m", "--message", default="backup agent skills")
    backup.set_defaults(func=cmd_backup)

    restore = subcommands.add_parser("restore", help="restore skills from a Synchro backup repo")
    add_root_args(restore)
    restore.add_argument("--repo", required=True, help="git-backed backup repo path")
    restore.add_argument("--from", dest="source", choices=TOOLS, required=True)
    restore.add_argument("--to", dest="target", choices=TOOLS, required=True)
    restore.add_argument("--name", action="append", help="skill name to restore; can be repeated")
    restore.add_argument("--backup-dir", default="~/.synchro/backups")
    restore.add_argument("--force", action="store_true", help="replace conflicting target skills after backup")
    restore.add_argument("--apply", action="store_true")
    restore.set_defaults(func=cmd_restore)

    sync = subcommands.add_parser("sync", help="sync skills from one install to another")
    add_root_args(sync)
    sync.add_argument("--from", dest="source", choices=TOOLS, required=True)
    sync.add_argument("--to", dest="target", choices=TOOLS, required=True)
    sync.add_argument("--name", action="append", help="skill name to sync; can be repeated")
    sync.add_argument("--backup-dir", default="~/.synchro/backups")
    sync.add_argument("--force", action="store_true", help="replace conflicting target skills after backup")
    sync.add_argument("--apply", action="store_true")
    sync.set_defaults(func=cmd_sync)

    migrate = subcommands.add_parser(
        "migrate-codex",
        help="consolidate legacy ~/.codex/skills user skills into ~/.agents/skills",
    )
    migrate.add_argument("--legacy-root", default=LEGACY_CODEX_ROOT)
    migrate.add_argument("--shared-root", default=DEFAULT_TOOL_ROOTS["codex"])
    migrate.add_argument("--name", action="append", help="skill name to migrate; can be repeated")
    migrate.add_argument("--backup-dir", default="~/.synchro/backups")
    migrate.add_argument("--apply", action="store_true")
    migrate.set_defaults(func=cmd_migrate_codex)

    doctor = subcommands.add_parser("doctor", help="inspect local Synchro tool roots")
    add_root_args(doctor)
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Peek at --config before the real parse so custom roots are registered in
    # time to appear as valid --root / --from / --to choices and --*-root flags.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config")
    pre_args, _ = pre.parse_known_args(argv)
    register_custom_roots(load_config_roots(pre_args.config))

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SynchroError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        # git failed under check=True; surface its captured stderr instead of a
        # bare traceback so the real cause (e.g. "Author identity unknown") shows.
        detail = (exc.stderr or "").strip() or f"command failed: {' '.join(str(a) for a in exc.cmd)}"
        print(f"error: git: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
