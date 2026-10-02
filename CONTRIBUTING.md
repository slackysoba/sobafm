# Contributing to SobaFM

Thanks for your interest in SobaFM. This guide explains how work is planned, proposed, and merged.

## Ways to contribute

- **Report a bug or suggest a feature** by [opening an issue](https://github.com/slackysoba/sobafm/issues/new/choose). Feature requests are weighed against the [requirements](docs/requirements.md) and the [roadmap](docs/roadmap.md).
- **Pick up an issue** labeled `good first issue` or `help wanted`. Comment on the issue before starting so work is not duplicated.
- **Report a vulnerability** privately, as described in the [security policy](SECURITY.md). Never open a public issue for one.

## How work is organized

- Work is tracked in the [SobaFM project](https://github.com/users/slackysoba/projects/2). Each milestone is an issue whose sub-issues are the pull-request-sized tasks, decisions, and research that deliver it. Only the current and next slices of the roadmap are broken into issues.
- Issues move through Backlog, Ready, In progress, In review, and Done, with Blocked for work waiting on a dependency or a maintainer decision. An issue is Ready when its outcome, scope, acceptance criteria, dependencies, and verification are defined and nothing blocks it.
- `task`, `bug`, `decision`, `research`, `enhancement`, and `milestone` labels give an issue's kind; `area:*` labels give the part of the system it touches; `maintainer-approval` marks work the maintainer must approve.
- [Requirements](docs/requirements.md), [architecture](docs/architecture.md), and the [roadmap](docs/roadmap.md) live in `docs/`. Material technical choices are recorded as [architecture decision records](docs/decisions/README.md): a `decision` issue frames the question, its pull request proposes the record, and merging the pull request accepts the decision.

## Development setup

SobaFM needs Python 3.14 and [uv](https://docs.astral.sh/uv/getting-started/installation/), which manages the environment from `uv.lock` and installs Python 3.14 if it is missing. SobaFM also needs the Opus library, which it checks at startup: `libopus0` on Debian and Ubuntu, or `opus` on Homebrew. On Apple silicon, discord.py looks only in the default library paths, which exclude Homebrew's, so link the library into one: `mkdir -p ~/lib && ln -s /opt/homebrew/lib/libopus.dylib ~/lib/`. On Windows, discord.py includes it for x86 and x64 Python; on Windows on Arm, use x64 Python. The Markdown hook also needs [Node.js](https://nodejs.org/) LTS, with npm, on your `PATH`.

```sh
uv sync                     # create .venv with the locked dependencies
uv run pre-commit install   # run the checks on every commit
cp .env.example .env        # then set DISCORD_TOKEN and GEMINI_API_KEY
uv run sobafm               # start SobaFM
```

On Windows with Smart App Control enabled, install Python 3.14 from [python.org](https://www.python.org/downloads/) and point uv at it (`uv sync --python <path to python.exe>`): uv's own Python builds are unsigned, and Smart App Control blocks some of their modules. If it also blocks a tool's launcher, run the tool as a module, for example `uv run python -m pytest`, and set `PYRIGHT_PYTHON_NODEJS_WHEEL=0` so pyright uses your installed Node.js instead of its bundled one.

The checks:

```sh
uv run ruff check                   # lint
uv run ruff format --check          # formatting; `uv run ruff format` applies it
uv run pyright                      # strict type checking
uv run pytest                       # tests
uv run pre-commit run --all-files   # every hook, including Markdown lint and a secret scan of staged changes
```

## Making a change

1. **Start from an issue.** Every change is linked to one. For anything beyond a small fix, agree on the approach in the issue before writing code.
2. **Branch** from `main`, or for a [stacked pull request](#stacked-pull-requests) from the branch it builds on, as `<type>/<issue>-<slug>`, where the type is `task`, `bug`, `decision`, or `research` (enhancements are implemented as tasks); for example, `task/8-python-toolchain`.
3. **Keep the change focused:** one issue, one branch, one pull request. Record unrelated findings as new issues.
4. **Reuse before building.** Prefer an existing platform capability or a well-maintained library over custom code, and explain the choice in the pull request. A new external service, which would add an account or a cost for operators, needs a decision first.
5. **Keep the repository consistent.** Update the documents the change affects and delete the code it supersedes, in the same pull request.
6. **Open a pull request** that follows the [template](.github/pull_request_template.md) and includes `Closes #<issue>`.

## Commits and pull requests

- Pull requests are squash-merged, and the pull request's title and description become the commit message.
- Titles are imperative and in sentence case, without a type prefix: "Add the deck frame buffer", not "feat: deck buffer".
- Descriptions state the intent, scope, verification, and risk of the change.
- Every required check must pass before merging: `ci` (lint, types, tests, Markdown lint, and link checks) and `security` (the vulnerability and license scan of `uv.lock`, and dependency review). Pull requests labeled `maintainer-approval` also need the maintainer's explicit approval. [Repository settings](docs/repository-settings.md) records the branch ruleset and every other setting.

### Stacked pull requests

A pull request can build on another that is still in review:

1. Branch from that pull request's branch, base the new pull request on it, and say in the description which pull request it builds on. Keep it rebased on that branch while both are in review.
2. Before merging the base pull request, change each dependent's base to `main`: `gh pr list --base <branch>` lists them, and `gh pr edit <number> --base main` changes one.
3. Merge with `gh pr merge <number> --squash`, without `--delete-branch`, and decline gh's offer to delete the branch. The repository deletes merged branches itself and retargets the pull requests based on them. When gh deletes the branch right after the merge, GitHub closes those pull requests instead ([cli/cli#14223](https://github.com/cli/cli/issues/14223)).
4. Rebase each dependent onto `main`, replaying only its own commits: `git rebase --onto origin/main <base head>`, where `<base head>` is the base pull request's last commit (`gh pr view <base> --json headRefOid --jq .headRefOid`). Then remove the note about the base from its description, which becomes the squash commit message.

If a dependent is closed anyway, do not push to its branch: a closed pull request cannot be reopened once its branch is force-pushed. Restore the base branch, reopen the dependent, change its base to `main`, and then rebase it.

## Templates and coding agents

Maintainers create milestone, task, decision, and research issues from the templates in [`docs/templates/`](docs/templates/), for example with `gh issue create --title "Add the deck frame buffer" --label task --body-file docs/templates/task.md`. Coding agents also follow [AGENTS.md](AGENTS.md).

## Code of conduct

Everyone taking part in SobaFM is expected to follow the [code of conduct](CODE_OF_CONDUCT.md).

## License

By contributing, you agree that your contributions are licensed under the project's [MIT License](LICENSE).
