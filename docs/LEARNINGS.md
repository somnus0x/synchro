# Synchro — Engineering Learnings

Consolidated reference of key decisions, fixes, and patterns learned during development.

---

## Migration

### Codex and Antigravity share the agent-skills user root

Codex discovers user skills from `~/.agents/skills`, which is also Antigravity's
skill root. Treating `~/.codex/skills` and `~/.agents/skills` as independent
writable installs caused copied skills to appear twice in Codex because matching
skill names are not merged during discovery.

```text
Codex + Antigravity user skills: ~/.agents/skills
Legacy Codex migration source:   ~/.codex/skills
Codex bundled system skills:     ~/.codex/skills/.system
```

Key takeaway: shared physical roots must be processed once; syncing between
`codex` and `agy` is a no-op, and legacy Codex user skills must be consolidated
with `migrate-codex` rather than copied between the two roots.

### Empty migration plans must exit before snapshot setup

After consolidation, the legacy root can still exist because Codex's `.system`
directory remains. An applied rerun then has no user-skill copy to create the
snapshot parent, so falling through to `snapshot.json` setup raises a filesystem
error instead of behaving idempotently.

Key takeaway: a migration with a valid but empty plan should return success
before initializing backup artifacts or mutation state.

## File Safety

### An excluded recovery file becomes deleted data

Sync/restore used the vault exclusion list for recovery backups and then removed
the entire displaced target. That kept `.env*` out of the backup directory but
silently deleted it from the live skill, so the safety copy could not actually
recover the target.

Key takeaway: before replacing a directory, distinguish disposable exclusions
from protected local data and refuse the replacement when protected entries
would be omitted from the recovery copy.
