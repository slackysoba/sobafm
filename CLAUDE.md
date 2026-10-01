# Claude Code instructions

@AGENTS.md

Claude-specific additions to the shared instructions imported above:

- Start sessions at the repository root, so Claude Code reads the shared permissions in `.claude/settings.json`. Personal preferences belong in the git-ignored `.claude/settings.local.json`.
- Auto memory is off for this repository, because it would keep notes that other contributors and tools cannot see.
- Claude Code's own worktrees live in the git-ignored `.claude/worktrees/`; name their branches `<type>/<issue>-<slug>` like any other.
