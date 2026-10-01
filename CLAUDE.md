# Claude Code instructions

@AGENTS.md

Claude-specific additions to the shared instructions imported above:

- Start sessions at the repository root, so Claude Code reads `.claude/settings.json` and resolves the import above.
- Shared permissions live in `.claude/settings.json`; personal preferences belong in the git-ignored `.claude/settings.local.json`.
- Auto memory is off for this repository, because it would keep notes that other contributors and tools cannot see. Record anything durable on the issue, on the pull request, or in the repository.
- Use Claude Code's worktree support for parallel work, and never share a worktree with another agent.
