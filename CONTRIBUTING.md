# Contributing

Read the [requirements](docs/requirements.md), [architecture](docs/architecture.md) and [engineering notes](docs/engineering-notes.md) before choosing a change. The application and development toolchain are not yet implemented.

## Scope and development

Take a Ready issue with satisfied dependencies and observable acceptance criteria. Use one cohesive change per branch and PR. Keep status, scope and evidence current.

Target Linux/Docker on amd64 and native arm64. Use maintained libraries, explicit ownership and narrowly justified dependencies. Avoid speculative abstractions and Windows runtime compatibility layers.

The toolchain work will establish Python 3.14, uv-locked dependencies, Ruff, strict Pyright, pre-commit and pytest. Once available, run:

```sh
uv sync --locked
uv run pre-commit run --all-files
uv run pytest
```

Keep live provider and Discord evaluations opt-in and outside ordinary CI. Prefer deterministic offline fixtures and failure-schedule regressions.

## Review and integration

Describe the behavior change, reason, scope, risk and validation. Link the closing issue. Update documentation and remove superseded implementation in the same PR.

Applicable checks and independent review must pass. Resolve findings publicly. Changes requiring maintainer approval must receive that approval. Squash merge; do not push directly to main after bootstrap or rewrite shared history.

For stacked PRs, retarget dependents before merging their base and retain required branches. Distinguish implementation, review, merge and release status.

## Decisions and safety

Record material product, architecture, service, cost, security or legal decisions with options, rationale and required evidence. Supersede accepted decision records rather than rewriting them.

Never commit secrets, real environment files or private operational notes. Use placeholders in examples. Do not start unapproved live provider, paid or voice tests. Review native components as well as Python dependencies.

Follow [AGENTS.md](AGENTS.md) for coordination, worker visibility, worktree ownership and development-storage controls. Keep personal filesystem locations and session-management notes out of public issue bodies when they are not needed for collaboration.
