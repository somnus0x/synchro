# Synchro

Synchro is a small local CLI for backing up agent skills into a git repo and
syncing skills between local agent installs.

MVP scope:

- audit Codex, Claude, Factory/Droid, and Agy skill roots
- back up skill directories into a git-backed vault
- restore skill directories from a git-backed vault
- sync missing or explicitly forced skills from one install to another
- default to dry-run; writes require `--apply`
- never push
- never dereference nested symlinks into a backup
- never copy `.env*`, `settings.local.json`, `.git`, caches, or bytecode into a vault or synced skill

## Install

Synchro requires Python 3.11 or newer.

The Python distribution is named `synchro-skills`; the installed command and
import package are both `synchro`.

```bash
python3 -m pip install .
synchro doctor
```

For development from a checkout, install editable with
`python3 -m pip install -e .`, run `python3 -m synchro`, or use the source
launcher at `./bin/synchro`.

## Commands

```bash
python3 -m synchro audit
python3 -m synchro backup --repo ~/agent-skill-vault
python3 -m synchro backup --repo ~/agent-skill-vault --apply --commit -m "backup skills"
python3 -m synchro restore --repo ~/agent-skill-vault --from codex --to claude
python3 -m synchro sync --from codex --to claude
python3 -m synchro sync --from claude --to agy --name mirror-sync
python3 -m synchro sync --from factory --to codex --apply
python3 -m synchro doctor
```

`sync` copies only missing skills by default. If a skill already exists at the
target with different bytes, Synchro reports a conflict and skips it. To
replace a conflicted target skill, pass `--force`; Synchro first writes a
timestamped, complete copy of the displaced target under
`~/.synchro/backups`. Local recovery backups are not Git-backed and retain
local-only files that are excluded from normal sync and vault copies.

`restore` follows the same safety rules as `sync`, but uses a Synchro backup
repo as the source. It is the cross-machine path: clone/pull your vault, run a
dry-run restore, then apply only the roots or skill names you want.

## Custom roots (config file)

If your skills live outside the default CLI roots — multiple skill repos, a
monorepo `skills/` dir, a private library — register them once in
`~/.synchro/config.json` instead of passing `--*-root` flags every run:

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

Custom root names may contain letters, numbers, underscores, and hyphens. The
names `all`, `factory_plugins`, and `factory-plugins` are reserved. Name
collisions are checked case-insensitively so a vault remains safe across macOS,
Windows, and Linux.

## Rename Migration

Existing users can move over without losing vault records. When the new config
is absent, Synchro still reads `~/.skillmine/config.json`. On the next applied
backup, it merges `skillmine-manifest.json` into `synchro-manifest.json` and
removes a valid legacy manifest only after the new file is written
successfully. An unreadable legacy manifest and existing recovery backups are
left untouched.

If the old Python distribution was installed, replace it explicitly:

```bash
python3 -m pip uninstall skillmine
python3 -m pip install .
```

## Roots

Default roots:

| Tool | Path |
| --- | --- |
| Codex | `~/.codex/skills` |
| Claude | `~/.claude/skills` |
| Factory/Droid personal skills | `~/.factory/skills` |
| Factory/Droid plugin skills | `~/.factory/plugins/marketplaces/*/plugins/*/skills` |
| Agy | `~/.agents/skills` |

`factory` uses Droid CLI's personal skill root as its write target. Synchro
also reads installed Droid plugin skills for audit and backup, but it refuses to
replace plugin-managed skills during `sync --to factory --force`; use a personal
skill or a proper Droid plugin instead.

If your Droid install uses another layout, pass `--factory-root` and/or
`--factory-plugins-root`.

Override roots when testing or using non-standard installs:

```bash
python3 -m synchro audit \
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
synchro-manifest.json
```

`--commit` stages and commits only the skill directories copied by that run and
the manifest. It does not push. Pre-existing staged files and edits under other
skill roots are left out of the backup commit.

Nested symlinks are preserved as symlinks, including cyclic links; Synchro
does not read or copy their target bytes. A top-level skill symlink that escapes
its configured root is rejected. A backup is also rejected before any write
when its source and destination overlap, when the vault's `skills/` root is a
symlink, or when the resolved destination would escape the vault.

## Exit Codes

- `0`: the requested operation completed and audit found no differences
- `1`: invalid input, a missing explicit root/name, or another operational error
- `2`: audit drift/missing skills or a sync/restore conflict

## Tests

```bash
python3 -m unittest -v
```

## Borrowed Patterns

Synchro intentionally borrows product patterns from existing tools without
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
- no secrets or local settings in Git vaults or synced copies
