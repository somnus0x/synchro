# Skillmine

Skillmine is a small local CLI for backing up agent skills into a git repo and
syncing skills between local agent installs.

MVP scope:

- audit Codex, Claude, Factory/Droid, and Agy skill roots
- back up skill directories into a git-backed vault
- restore skill directories from a git-backed vault
- sync missing or explicitly forced skills from one install to another
- default to dry-run; writes require `--apply`
- never push
- never copy `.env*`, `settings.local.json`, `.git`, caches, or bytecode

## Commands

```bash
python3 -m skillmine audit
python3 -m skillmine backup --repo ~/agent-skill-vault
python3 -m skillmine backup --repo ~/agent-skill-vault --apply --commit -m "backup skills"
python3 -m skillmine restore --repo ~/agent-skill-vault --from codex --to claude
python3 -m skillmine sync --from codex --to claude
python3 -m skillmine sync --from claude --to agy --name mirror-sync
python3 -m skillmine sync --from factory --to codex --apply
python3 -m skillmine doctor
```

`sync` copies only missing skills by default. If a skill already exists at the
target with different bytes, Skillmine reports a conflict and skips it. To
replace a conflicted target skill, pass `--force`; Skillmine first writes a
timestamped backup under `~/.skillmine/backups`.

`restore` follows the same safety rules as `sync`, but uses a Skillmine backup
repo as the source. It is the cross-machine path: clone/pull your vault, run a
dry-run restore, then apply only the roots or skill names you want.

## Custom roots (config file)

If your skills live outside the default CLI roots — multiple skill repos, a
monorepo `skills/` dir, a private library — register them once in
`~/.skillmine/config.json` instead of passing `--*-root` flags every run:

```json
{
  "roots": {
    "thufir": "/root/thufir-skills",
    "privatelib": "/root/private-loop-library/skills"
  }
}
```

Custom roots become first-class named tools everywhere: `audit`, `doctor`,
`backup --root thufir`, `sync --from privatelib --to codex`, etc. They only ADD
roots — a name that collides with a built-in (`codex`/`claude`/`factory`/`agy`)
is refused, so config can never silently rebind a default. Override the config
path with `--config`; an explicit `--config` that's missing is an error, while
the default path being absent is fine (config is optional).

## Roots

Default roots:

| Tool | Path |
| --- | --- |
| Codex | `~/.codex/skills` |
| Claude | `~/.claude/skills` |
| Factory/Droid personal skills | `~/.factory/skills` |
| Factory/Droid plugin skills | `~/.factory/plugins/marketplaces/*/plugins/*/skills` |
| Agy | `~/.agents/skills` |

`factory` uses Droid CLI's personal skill root as its write target. Skillmine
also reads installed Droid plugin skills for audit and backup, but it refuses to
replace plugin-managed skills during `sync --to factory --force`; use a personal
skill or a proper Droid plugin instead.

If your Droid install uses another layout, pass `--factory-root` and/or
`--factory-plugins-root`.

Override roots when testing or using non-standard installs:

```bash
python3 -m skillmine audit \
  --codex-root /tmp/codex-skills \
  --claude-root /tmp/claude-skills \
  --factory-root /tmp/factory-skills \
  --factory-plugins-root /tmp/factory-plugin-marketplace \
  --agy-root /tmp/agy-skills
```

## Git Backup

`backup --apply` creates the repo path if needed, initializes git when no
`.git` directory exists, and writes:

```text
skills/
  codex/<skill-name>/
  claude/<skill-name>/
  factory/<skill-name>/
  agy/<skill-name>/
skillmine-manifest.json
```

`--commit` stages and commits the backup locally. It does not push.

## Borrowed Patterns

Skillmine intentionally borrows product patterns from existing tools without
copying their code:

- skills.sh: multi-agent skill installation targets
- Skilldex: manifests, scopes, and skill-package thinking
- Factory Droid: personal skills vs plugin-managed skills
- chezmoi/yadm: dry-run-first config sync across machines
- Mackup: backup/restore wording and safety shape
- GNU Stow: future symlink-farm install mode

See [docs/INSPIRATIONS.md](docs/INSPIRATIONS.md).

## Non-Goals

- no global `AGENTS.md` or `CLAUDE.md` sync yet
- no automatic scheduled sync
- no bidirectional merge of conflicting skills
- no secret or local settings backup
