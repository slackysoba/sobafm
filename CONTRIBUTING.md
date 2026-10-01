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

## Making a change

1. **Start from an issue.** Every change is linked to one. For anything beyond a small fix, agree on the approach in the issue before writing code.
2. **Branch** from `main` as `<type>/<issue>-<slug>`, where the type is `task`, `bug`, `decision`, or `research` (enhancements are implemented as tasks); for example, `task/8-python-toolchain`.
3. **Keep the change focused:** one issue, one branch, one pull request. Record unrelated findings as new issues.
4. **Reuse before building.** Prefer an existing platform capability or a well-maintained library over custom code, and explain the choice in the pull request. A new external service, which would add an account or a cost for operators, needs a decision first.
5. **Keep the repository consistent.** Update the documents the change affects and delete the code it supersedes, in the same pull request.
6. **Open a pull request** that describes the change and includes `Closes #<issue>`.

## Commits and pull requests

- Pull requests are squash-merged, and the pull request's title and description become the commit message.
- Titles are imperative and in sentence case, without a type prefix: "Add the deck frame buffer", not "feat: deck buffer".
- Descriptions state the intent, scope, verification, and risk of the change.
- Every required check must pass before merging. Pull requests labeled `maintainer-approval` also need the maintainer's explicit approval.

## Code of conduct

Everyone taking part in SobaFM is expected to follow the [code of conduct](CODE_OF_CONDUCT.md).

## License

By contributing, you agree that your contributions are licensed under the project's [MIT License](LICENSE).
