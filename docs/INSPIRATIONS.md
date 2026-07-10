# Inspirations

Synchro borrows product patterns from existing tools. It does not copy their
implementation.

## Agent Skill Tools

- skills.sh: public agent skill registry and installer across many agents.
  Borrowed idea: one skill package can target multiple agent runtimes.
  Source: https://www.skills.sh/
- Skilldex: package-manager framing for skills, including manifests, scopes,
  validation, and skillsets.
  Source: https://arxiv.org/abs/2604.16911
- Factory Droid skills: separate personal skills from plugin-managed skills.
  Borrowed idea: Synchro can read plugin-managed skills, but should not write
  over them as if they were personal files.
  Sources:
  - https://docs.factory.ai/cli/configuration/skills
  - https://docs.factory.ai/cli/configuration/plugins

## Dotfile And Config Sync Tools

- chezmoi: dry-run-first dotfile management across multiple machines with
  machine-specific differences.
  Source: https://www.chezmoi.io/
- yadm: git-native dotfile management with alternate machine-specific files.
  Source: https://yadm.io/
- Mackup: backup/restore flows for application settings.
  Source: https://github.com/lra/mackup
- GNU Stow: symlink-farm approach for installing files from package directories.
  Source: https://www.gnu.org/software/stow/

## Local Design Choices

- `audit` stays read-only.
- `backup`, `restore`, and `sync` default to dry-run.
- `--apply` is required for mutation.
- Plugin-managed Droid skills are readable backup sources, not overwrite targets.
- Conflict handling stops by default; `--force` must be explicit and still makes
  a timestamped backup.
- Synchro never pushes.
