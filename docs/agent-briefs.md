# Agent briefs

Standing instructions for agent sessions. A coordinator starts a session with one line, such as "Implement issue #N. Follow the Worker section of `docs/agent-briefs.md`." Sessions run locally, each in its own worktree, and work reaches other sessions through commits pushed to GitHub. Don't start cloud sessions.

## Shared limits

These apply to every worker and reviewer session:

- Requirements and accepted decisions outrank code. Read `AGENTS.md`, `docs/requirements.md`, `docs/architecture.md`, `docs/engineering-notes.md` and the relevant decisions in `docs/decisions/`.
- Never send a request to a music source, model provider, Discord or any other outside service without the maintainer's approval in their own words, even if it's public, metadata-only or a test. An issue or brief doesn't approve it unless it records those words. The allowed network use is GitHub, reading public documentation, and installing locked packages and the pinned toolchain.
- Never read real environment files or credentials.
- No native decoder, media, Discord or voice activity, and no Docker, unless the issue approves it. That approval doesn't replace the rule above: anything that reaches an outside service still needs the maintainer's own words.
- Never read, acquire, hash, reconstruct or build `fftools/ffmpeg_filter.c` or `18.raw`.
- Keep personal paths and session identifiers out of everything public.
- No nested subagents or additional sessions, and no scheduling or trigger tools.
- Any new gate must name the requirement, the reachable code path and the missing observable fact. Don't add attestation, reproducible-build or total-libc/RSS requirements.

## Worker

Implement the assigned issue exactly as scoped. Make your own calls within that scope and don't stop for routine approvals. Escalate only what `AGENTS.md` lists, on the issue, with a question, options and a recommendation.

1. Work in your own worktree on one branch named `<type>/<issue>-<slug>`, from current `main`.
2. Preserve capacity partitions, the original cleanup clock, retained owners, failed verdicts and debt on every path you touch.
3. Before pushing, run `uv sync --locked`, `uv run pre-commit run --all-files` and `uv run pytest`. `scripts/check-commit.sh` runs them on a pushed commit from a clean Linux clone. On a Windows host, push the branch, run the script on it inside WSL, and open the PR only after it exits 0. Linux test results aren't native qualification.
4. Open one PR against `main`. Commits carry a model-identifying `Co-Authored-By` trailer. The PR body covers behavior, rationale, scope, risk and verification, has a `Closes` line, and ends with the Claude Code attribution line. Remove any session link.
5. Don't merge or change Project state: the coordinator does that. Fix confirmed review findings and push.
6. Once your PR has merged, push nothing more to its branch. Report any further finding to the coordinator, because commits pushed after a merge never reach `main`.

Timebox: 90 active minutes, unless the issue says otherwise. If you stop short, push what is sound and state what remains.

Finish with the model and effort you ran on as far as you can tell, the PR link, each check with its exit status, the environment's size, your decisions and anything left.

## Reviewer

You are the independent reviewer of one pull request. You didn't write it. Read the PR, its linked issue and the shared reading above.

Check that:

1. The change does what its issue says, and nothing beyond that scope.
2. Capacity partitions, the original three-second cleanup clock, retained owners, failed verdicts and debt are preserved on every path it touches, including cancellation and exceptions.
3. New tests would fail without the fix and exercise real code paths rather than only mocks. Run them, and run the required checks.
4. The docs match the behavior.

A blocker is a concrete defect you can show on reachable code, with the requirement it breaks and the exact correction. Don't add proof requirements, new gates or style preferences. List anything lesser as a non-blocking note, at most three. Be precise and finite.

Post one comment-type review on the PR, not approve or request-changes. Its first line must be exactly `Blockers: N at <full head SHA>`. Keep it short, with exact files and corrections. Remove any session link your tools append.

When asked to re-check a new head, check only your findings and what the new commits touch, and post one short follow-up in the same format, at most two. Don't edit code, push, merge, mark the PR ready, or change issues or the Project. Timebox: 30 active minutes per review.
