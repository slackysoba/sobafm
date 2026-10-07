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

The evaluation set measures how well the interpreter turns requests into music plans. It calls Gemini with your `GEMINI_API_KEY`, so `uv run pytest` and CI skip it. Run it after changing the interpreter's instruction, schema, or model, and record the results on the pull request. Set `SOBAFM_GEMINI_MODEL` to evaluate another model.

```sh
uv run --env-file .env pytest -m eval -s   # about 4 minutes, plus 1 to 2 for each retry
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
3. Merge with `gh pr merge <number> --squash`, without `--delete-branch`. The repository deletes merged branches itself and retargets the pull requests based on them. When gh deletes the branch right after the merge, GitHub closes those pull requests instead ([cli/cli#14223](https://github.com/cli/cli/issues/14223)).
4. Rebase each dependent onto `main`, replaying only its own commits: after `git fetch origin`, run `git rebase --onto origin/main <base head>`, where `<base head>` is the base pull request's last commit (`gh pr view <base> --json headRefOid --jq .headRefOid`), and push with `--force-with-lease`. Then remove the note about the base from its description, which becomes the squash commit message.

If a dependent is closed anyway, restore the base branch, reopen the dependent, change its base to `main`, delete the restored branch, and then rebase the dependent. GitHub reopens a pull request only while its branch is at the commit it had when it closed, so if the branch has been pushed since, first force-push it back to that commit (`gh pr view <number> --json headRefOid --jq .headRefOid`).

## Releasing

Only the maintainer creates release tags. [ADR-0005](docs/decisions/0005-release-images-with-github-actions-and-artifact-attestations.md) defines publication; [the release checklist](https://github.com/slackysoba/sobafm/issues/108) tracks the M4 exit evidence. Before the first release tag, the maintainer applies and reads back both [release tag rulesets](docs/repository-settings.md#release-tag-rulesets). The creation restriction permits only that maintainer; the independent update/deletion restriction has no bypass actors.

1. Open and merge a version-bump pull request. Change `project.version` and update `uv.lock` with `uv lock`; run the full checks. The workflow compares the tag without `v` and the tagged project version using `packaging.version.Version`, so `v1.0.0-rc.1` matches `1.0.0rc1`.
2. Use exactly three numeric release components (`X.Y.Z`) before an optional PEP 440 prerelease, development, or postrelease suffix. The text after `v` must also be a valid Docker tag, at most 128 characters: epochs (`!`), local versions (`+`), whitespace, and slashes are rejected. Three components keep the full version tag distinct from a moving `X.Y` alias.
3. Confirm the source commit is on `main`, its required checks pass, and the merged pull requests carry the labels used by [the release-note configuration](.github/release.yml). Choose a prerelease first for #104's publication check and #107's soak test. For example, **after** the approved version-bump PR sets `1.0.0rc1`:

   ```sh
   git fetch origin main
   git switch main
   git pull --ff-only
   git tag -a v1.0.0-rc.1 -m "SobaFM 1.0.0 release candidate 1"
   git push origin refs/tags/v1.0.0-rc.1
   ```

4. Follow the `Release` workflow, then inspect its GitHub release and image digest. The workflow builds AMD64 and ARM64 once, publishes only the full version tag first, signs the index digest, and verifies its source and both platform attestations before promoting aliases. It generates label-grouped notes; prereleases are explicitly marked and never receive `X.Y` or `latest`.
5. On first publication, complete the maintainer's [public GHCR bootstrap and anonymous pulls](docs/repository-settings.md#container-package-and-release-permissions). Verify [the image's source identity and both platform SBOMs](docs/self-hosting.md#verify-the-image-you-pulled), and record the tag, source commit, run, digest, and results on #104. A successful offline check or an ordinary PR CI run is not the required prerelease dry run.
6. After the clean-machine guide test, release-image soak, and other [M4 exit criteria](https://github.com/slackysoba/sobafm/issues/4) pass, merge the stable version-bump PR and create its stable tag using the same process. Inspect the generated notes and record the final release evidence on #108.

Publication is serialized across tags and reruns with `queue: max`, retaining up to 100 pending runs. Since queue ordering is not version ordering, the workflow refreshes protected tags and compares valid stable PEP 440 versions: `X.Y` advances only to the highest version in that minor line, and `latest` only to the highest stable version globally. GitHub release latest status uses the same global guard. Invalid tags, tags off `main`, and tags that do not match their own source version never participate in the comparison. An older rerun cannot move a newer alias backward; a prerelease receives only its own version tag. Full tags preserve their spelling after the leading `v`, including prerelease separators.

Rerun a failed tag workflow after fixing an operational problem such as registry visibility. Existing release notes are preserved on reruns. If source or version is wrong, fix it through a new pull request and create a new tag: protected tags cannot be moved or deleted. #104 stays open until the real prerelease publication and attestation verification succeed.

## Templates and coding agents

Maintainers create milestone, task, decision, and research issues from the templates in [`docs/templates/`](docs/templates/), for example with `gh issue create --title "Add the deck frame buffer" --label task --body-file docs/templates/task.md`. Coding agents also follow [AGENTS.md](AGENTS.md).

## Code of conduct

Everyone taking part in SobaFM is expected to follow the [code of conduct](CODE_OF_CONDUCT.md).

## License

By contributing, you agree that your contributions are licensed under the project's [MIT License](LICENSE).
