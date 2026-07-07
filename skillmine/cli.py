from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


DEFAULT_TOOL_ROOTS = {
    "codex": "~/.codex/skills",
    "claude": "~/.claude/skills",
    "factory": "~/.factory/skills",
    "agy": "~/.agents/skills",
}

# Runtime root map. Extended by ~/.skillmine/config.json custom roots so a machine
# with skills in non-default locations (multiple skill repos, a monorepo dir) can
# register them as first-class named sources/targets. Defaults are never overwritten.
TOOL_ROOTS = dict(DEFAULT_TOOL_ROOTS)

TOOLS = tuple(TOOL_ROOTS)

DEFAULT_CONFIG_PATH = "~/.skillmine/config.json"


def load_config_roots(config_path: str | None) -> dict[str, str]:
    """Read custom named roots from a skillmine config file.

    Config shape:
        {"roots": {"thufir": "/root/thufir-skills", "lib": "/root/lib/skills"}}

    Returns an empty dict when the file is absent (config is optional). A custom
    root whose name collides with a built-in tool is refused, so config can only
    ADD roots, never silently rebind codex/claude/factory/agy.
    """
    path = expand_path(config_path or DEFAULT_CONFIG_PATH)
    if not path.exists():
        if config_path is not None:
            raise SystemExit(f"config not found: {path}")
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"invalid config {path}: {exc}")
    roots = data.get("roots", {})
    if not isinstance(roots, dict):
        raise SystemExit(f"invalid config {path}: 'roots' must be an object")
    custom: dict[str, str] = {}
    for name, value in roots.items():
        if name in DEFAULT_TOOL_ROOTS:
            raise SystemExit(
                f"invalid config {path}: '{name}' collides with a built-in tool; "
                "custom roots must use a distinct name"
            )
        if not isinstance(value, str):
            raise SystemExit(f"invalid config {path}: root '{name}' must be a string path")
        custom[name] = value
    return custom


def register_custom_roots(custom: dict[str, str]) -> None:
    """Merge custom roots into the runtime TOOL_ROOTS / TOOLS tables."""
    global TOOLS
    TOOL_ROOTS.update(custom)
    TOOLS = tuple(TOOL_ROOTS)

EXCLUDES = {
    ".git",
    ".DS_Store",
    "__pycache__",
    "*.pyc",
    ".env*",
    "settings.local.json",
}


@dataclass(frozen=True)
class Skill:
    tool: str
    name: str
    path: Path
    digest: str
    managed: bool = False


def expand_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def is_excluded(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in EXCLUDES)


def iter_skill_files(skill_dir: Path) -> Iterable[Path]:
    root = skill_dir.resolve()
    stack = [root]
    while stack:
        current = stack.pop()
        for child in sorted(current.iterdir(), key=lambda p: p.name):
            if is_excluded(child.name):
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
    if not root.exists():
        return {}

    skills: dict[str, Skill] = {}
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if is_excluded(entry.name) or not entry.is_dir():
            continue
        if not (entry / "SKILL.md").exists():
            continue
        skills[entry.name] = make_skill(tool, entry.name, entry, managed=managed)
    return skills


def discover_factory_plugin_skills(plugin_root: Path) -> dict[str, Skill]:
    if (plugin_root / "plugins").exists():
        marketplace_roots = [plugin_root]
    elif plugin_root.exists():
        marketplace_roots = [entry for entry in sorted(plugin_root.iterdir(), key=lambda p: p.name) if (entry / "plugins").exists()]
    else:
        marketplace_roots = []

    if not marketplace_roots:
        return {}

    skills: dict[str, Skill] = {}
    for marketplace_root in marketplace_roots:
        for plugin_dir in sorted((marketplace_root / "plugins").iterdir(), key=lambda p: p.name):
            skills_dir = plugin_dir / "skills"
            if not skills_dir.exists():
                continue
            for entry in sorted(skills_dir.iterdir(), key=lambda p: p.name):
                if is_excluded(entry.name) or not entry.is_dir():
                    continue
                if not (entry / "SKILL.md").exists():
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


def backup_path(base: Path, tool: str, name: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return base / timestamp / tool / name


def copy_skill(src: Path, dest: Path) -> None:
    if dest.exists() or dest.is_symlink():
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        else:
            shutil.rmtree(dest)
    shutil.copytree(src.resolve(), dest, symlinks=False, ignore=copy_ignore)


def copy_ignore(_dir: str, names: list[str]) -> set[str]:
    return {name for name in names if is_excluded(name)}


def run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
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


def cmd_audit(args: argparse.Namespace) -> int:
    roots = roots_from_args(args)
    active_tools = selected_tools(args)
    all_skills = {tool: discover_skills(tool, roots) for tool in active_tools}

    for tool in active_tools:
        root = roots[tool]
        skills = all_skills[tool]
        status = "missing root" if not root.exists() else f"{len(skills)} skills"
        if tool == "factory" and roots["factory_plugins"].exists():
            plugin_count = sum(1 for skill in skills.values() if skill.managed)
            status = f"{status}, {plugin_count} plugin skills"
        print(f"{tool}: {root} ({status})")

    comparable_tools = [tool for tool in active_tools if roots[tool].exists() or all_skills[tool]]
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
    return 0 if drift == 0 else 2


def selected_tools(args: argparse.Namespace) -> list[str]:
    if args.root == "all":
        return list(TOOLS)
    return [args.root]


def load_existing_manifest(repo: Path) -> dict[str, object]:
    """Read an existing backup manifest, or an empty skeleton if none.

    A backup vault can be written by more than one machine. The manifest is the
    repo's index of what it holds, so a partial backup (`--root thufir`) must
    MERGE into it, not overwrite the entries other machines/roots wrote. Returns
    the parsed manifest with `roots`/`skills` present; on any read/parse error it
    starts fresh (a corrupt index shouldn't block a backup).
    """
    manifest_path = repo / "skillmine-manifest.json"
    if not manifest_path.exists():
        return {"roots": {}, "skills": []}
    try:
        data = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"roots": {}, "skills": []}
    if not isinstance(data, dict):
        return {"roots": {}, "skills": []}
    data.setdefault("roots", {})
    data.setdefault("skills", [])
    return data


def merge_manifest(existing: dict[str, object], new_roots: dict[str, str], new_skills: list[dict]) -> dict[str, object]:
    """Merge this run's roots+skills over an existing manifest, keyed by `dest`.

    `dest` is the repo-relative identity of a backed-up skill, so same-dest
    entries from this run replace their prior record while every other entry
    (other roots, other machines) is preserved. Roots union the same way.
    Does NOT prune skills whose source vanished — that's an explicit --prune
    concern, out of scope here.
    """
    by_dest: dict[str, dict] = {entry["dest"]: entry for entry in existing.get("skills", []) if "dest" in entry}
    for entry in new_skills:
        by_dest[entry["dest"]] = entry
    merged_roots = dict(existing.get("roots", {}))
    merged_roots.update(new_roots)
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "roots": merged_roots,
        "skills": sorted(by_dest.values(), key=lambda e: e["dest"]),
    }


def cmd_backup(args: argparse.Namespace) -> int:
    roots = roots_from_args(args)
    repo = expand_path(args.repo)
    tools = selected_tools(args)
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
            dest = repo / "skills" / tool / skill.name
            planned.append((skill, dest))
            new_skills.append(
                {
                    "tool": tool,
                    "name": skill.name,
                    "source": str(skill.path),
                    "dest": str(dest),
                    "sha256": skill.digest,
                }
            )

    for skill, dest in planned:
        print(f"{'copy' if args.apply else 'would copy'}: {skill.tool}/{skill.name} -> {dest}")

    if not args.apply:
        print("dry-run: pass --apply to write backup repo")
        return 0

    ensure_git_repo(repo)
    for skill, dest in planned:
        dest.parent.mkdir(parents=True, exist_ok=True)
        copy_skill(skill.path, dest)

    manifest = merge_manifest(load_existing_manifest(repo), new_roots, new_skills)
    manifest_path = repo / "skillmine-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    if args.commit:
        run_git(repo, "add", "skills", "skillmine-manifest.json")
        diff = run_git(repo, "diff", "--cached", "--quiet", check=False)
        if diff.returncode == 0:
            print("git: no backup changes to commit")
        else:
            run_git(repo, "commit", "-m", args.message)
            print(f"git: committed backup in {repo}")

    return 0


def discover_vault_skills(repo: Path, tool: str) -> dict[str, Skill]:
    return discover_flat_skills(tool, repo / "skills" / tool)


def cmd_restore(args: argparse.Namespace) -> int:
    roots = roots_from_args(args)
    repo = expand_path(args.repo)
    source_skills = discover_vault_skills(repo, args.source)
    target_skills = discover_skills(args.target, roots)
    selected_names = set(args.name or source_skills.keys())
    backup_base = expand_path(args.backup_dir)
    actions = 0
    conflicts = 0

    if not source_skills:
        print(f"missing backup source: {repo / 'skills' / args.source}")
        return 1

    for name in sorted(selected_names):
        source = source_skills.get(name)
        if not source:
            print(f"missing backup skill: {args.source}/{name}")
            continue

        target = target_skills.get(name)
        target_path = roots[args.target] / name
        if target and target.digest == source.digest:
            print(f"same: {name}")
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

        verb = "replace" if target else "restore"
        print(f"{verb if args.apply else 'would ' + verb}: {repo}/skills/{args.source}/{name} -> {target_path}")
        actions += 1

        if not args.apply:
            continue

        roots[args.target].mkdir(parents=True, exist_ok=True)
        if target_path.exists() or target_path.is_symlink():
            backup_dest = backup_path(backup_base, args.target, name)
            backup_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(target_path.resolve(), backup_dest, symlinks=False, ignore=copy_ignore)
            print(f"backup: {target_path} -> {backup_dest}")
        copy_skill(source.path, target_path)

    if not args.apply:
        print("dry-run: pass --apply to restore target skills")

    if conflicts:
        print(f"summary: actions={actions} conflicts={conflicts}")
        return 2

    print(f"summary: actions={actions} conflicts=0")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    if args.source == args.target:
        print("--from and --to must be different tools", file=sys.stderr)
        return 1

    roots = roots_from_args(args)
    source_skills = discover_skills(args.source, roots)
    target_skills = discover_skills(args.target, roots)
    selected_names = set(args.name or source_skills.keys())
    backup_base = expand_path(args.backup_dir)
    actions = 0
    conflicts = 0

    for name in sorted(selected_names):
        source = source_skills.get(name)
        if not source:
            print(f"missing source: {args.source}/{name}")
            continue

        target = target_skills.get(name)
        target_path = roots[args.target] / name
        if target and target.digest == source.digest:
            print(f"same: {name}")
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

        verb = "replace" if target else "copy"
        print(f"{verb if args.apply else 'would ' + verb}: {args.source}/{name} -> {target_path}")
        actions += 1

        if not args.apply:
            continue

        roots[args.target].mkdir(parents=True, exist_ok=True)
        if target_path.exists() or target_path.is_symlink():
            backup_dest = backup_path(backup_base, args.target, name)
            backup_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(target_path.resolve(), backup_dest, symlinks=False, ignore=copy_ignore)
            print(f"backup: {target_path} -> {backup_dest}")
        copy_skill(source.path, target_path)

    if not args.apply:
        print("dry-run: pass --apply to write target skills")

    if conflicts:
        print(f"summary: actions={actions} conflicts={conflicts}")
        return 2

    print(f"summary: actions={actions} conflicts=0")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    roots = roots_from_args(args)
    print(f"skillmine: {Path(__file__).resolve().parents[1]}")
    git = shutil.which("git")
    droid = shutil.which("droid")
    print(f"git: {git or 'missing'}")
    print(f"droid: {droid or 'missing'}")

    for tool in TOOLS:
        skills = discover_skills(tool, roots)
        root = roots[tool]
        status = "exists" if root.exists() else "missing"
        detail = f"{len(skills)} skills"
        if tool == "factory" and roots["factory_plugins"].exists():
            plugin_count = sum(1 for skill in skills.values() if skill.managed)
            detail = f"{detail}, {plugin_count} plugin-managed"
        print(f"{tool}: {root} ({status}, {detail})")

    if roots["factory_plugins"].exists():
        print(f"factory plugins: {roots['factory_plugins']} (exists)")
    else:
        print(f"factory plugins: {roots['factory_plugins']} (missing)")

    return 0


def add_root_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        default=None,
        help=f"skillmine config file with custom named roots (default: {DEFAULT_CONFIG_PATH})",
    )
    for tool, default in TOOL_ROOTS.items():
        parser.add_argument(f"--{tool}-root", default=default)
    parser.add_argument(
        "--factory-plugins-root",
        default="~/.factory/plugins/marketplaces",
        help="Factory/Droid plugin marketplaces root, or one marketplace root, to read installed plugin skills from",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skillmine")
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

    restore = subcommands.add_parser("restore", help="restore skills from a Skillmine backup repo")
    add_root_args(restore)
    restore.add_argument("--repo", required=True, help="git-backed backup repo path")
    restore.add_argument("--from", dest="source", choices=TOOLS, required=True)
    restore.add_argument("--to", dest="target", choices=TOOLS, required=True)
    restore.add_argument("--name", action="append", help="skill name to restore; can be repeated")
    restore.add_argument("--backup-dir", default="~/.skillmine/backups")
    restore.add_argument("--force", action="store_true", help="replace conflicting target skills after backup")
    restore.add_argument("--apply", action="store_true")
    restore.set_defaults(func=cmd_restore)

    sync = subcommands.add_parser("sync", help="sync skills from one install to another")
    add_root_args(sync)
    sync.add_argument("--from", dest="source", choices=TOOLS, required=True)
    sync.add_argument("--to", dest="target", choices=TOOLS, required=True)
    sync.add_argument("--name", action="append", help="skill name to sync; can be repeated")
    sync.add_argument("--backup-dir", default="~/.skillmine/backups")
    sync.add_argument("--force", action="store_true", help="replace conflicting target skills after backup")
    sync.add_argument("--apply", action="store_true")
    sync.set_defaults(func=cmd_sync)

    doctor = subcommands.add_parser("doctor", help="inspect local Skillmine tool roots")
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
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
