# Contributing

Read the [requirements](docs/requirements.md), [architecture](docs/architecture.md) and [engineering notes](docs/engineering-notes.md) before choosing a change. The development toolchain is available; application playback is not yet implemented.

## Scope and development

Take a Ready issue with satisfied dependencies and observable acceptance criteria. Use one cohesive change per branch and PR. Keep status, scope and evidence current.

Target Linux/Docker on amd64 and native arm64. Use maintained libraries, explicit ownership and narrowly justified dependencies. Avoid speculative abstractions and Windows runtime compatibility layers.

## Linux development setup

Install Git and [uv 0.13.6](https://docs.astral.sh/uv/getting-started/installation/) in your Linux development environment. Use the versioned installer or package from that release; `pyproject.toml` enforces this uv version. From a fresh checkout:

```sh
uv python install
uv sync --locked
```

`.python-version` selects Python 3.14, and the project excludes other minor versions. `uv sync --locked` installs the package and development tools into `.venv` and refuses a stale lockfile. No application credentials are needed. The optional `uv run pre-commit install` command installs a local Git hook; the explicit checks below are required regardless of hook installation.

Before every push, run these commands from the repository root:

```sh
uv sync --locked
uv run pre-commit run --all-files
uv run pytest
```

Pre-commit checks Ruff lint/formatting, strict Pyright over `src` and `tests`, Markdown, secrets, YAML/TOML syntax, merge conflicts, whitespace and final newlines. Its local hooks use the uv-locked environment in offline mode, without separate hook environments; the Pyright wrapper's online update notice is disabled. Markdown line length is disabled to preserve the existing prose and tables. The PR template is exempt from the top-level-title rule because GitHub supplies its title. The inline-HTML rule allows only the additional elements `type`, `issue` and `slug` used as branch placeholders in `AGENTS.md`. Other structural rules remain enabled. Secret scanning uses `--no-verify` to prevent network verification and excludes only the hash-heavy `uv.lock`. Review every finding; do not generate a blanket secret baseline.

CI runs the same three commands on native Ubuntu amd64 and arm64 runners. Its check names are `Linux checks (amd64)` and `Linux checks (arm64)`. The checks cover development tooling and the installed-package smoke test; they do not establish player behavior, Linux resource containment, music quality or release readiness. CI installs dependencies with network access, but no hook or test needs provider access. CI uses read-only repository permissions and no persisted checkout credentials.

Keep `tests/` entirely offline. Place future live evaluations outside that collection tree and require explicit authorization and invocation. Do not collect a live test and rely only on a marker to prevent its imports or fixtures from contacting a provider. Prefer deterministic owned fixtures and failure-schedule regressions. No provider, Discord, audio acquisition or FFmpeg build is part of these checks.

Use separate `.venv` directories for concurrent worktrees. uv's package cache may be reused; do not share a mutable environment between active branches. Follow the storage agreement in the assigned issue before installation or builds. The current smoke test imports the installed package from a separate directory with Python's isolated mode, so a checkout-only import cannot conceal a missing installation.

## Tool choices and updates

The initial development dependency review was performed on 2026-10-27 against upstream documentation, release metadata and license files. These tools have no application-runtime dependency role:

| Tool | Pin | Rationale and maintenance/license assessment |
| --- | --- | --- |
| [uv / uv_build](https://docs.astral.sh/uv/concepts/build-backend/) | 0.13.6 | Actively released by Astral; MIT or Apache-2.0. One manager, lockfile and bundled pure-Python build backend; no native application build system. |
| [Ruff](https://docs.astral.sh/ruff/) | 0.17.3 | Actively released by Astral; MIT. One linter/formatter with Python 3.14 support. |
| [Pyright wrapper](https://github.com/RobertCraigie/pyright-python) / [Pyright](https://github.com/microsoft/pyright) | 1.1.414 | MIT. The maintained Python wrapper bundles Microsoft's checker. Its recommended `nodejs` extra locks the [Node binary wheel](https://github.com/njzjz/nodejs-wheel) (24.19.0, MIT plus bundled third-party notices), avoiding an unpinned runtime download. This is development-only native tooling, not an audio component. |
| [pre-commit](https://github.com/pre-commit/pre-commit) / [pre-commit-hooks](https://github.com/pre-commit/pre-commit-hooks) | 4.7.0 / 6.0.0 | Maintained upstream projects; MIT. Runs the common locked tools and standard file checks. |
| [PyMarkdown](https://github.com/jackdewinter/pymarkdown) | 0.9.41 | Actively released; MIT. Checks Markdown without another JavaScript toolchain. |
| [detect-secrets](https://github.com/Yelp/detect-secrets) | 1.5.0 | Apache-2.0. Mature scanner; latest published release is from May 2024, with upstream activity in April 2026. Its slower release cadence is a maintenance risk to revisit. Offline heuristic detection is defense in depth, not proof that a tree contains no secrets. |
| [pytest](https://docs.pytest.org/en/stable/) | 9.1.1 | Maintained project; MIT. Strict collection/configuration and deterministic offline regression tests. |

Direct tool pins and all transitive distributions/hashes are recorded in `uv.lock`; the build backend and uv version are pinned in `pyproject.toml`. GitHub Actions are pinned to verified commit IDs. PyPI's advisory metadata had no entries for the selected direct pins at review time; this limited check is not a complete security audit and does not cover bundled Node components or future application/native dependencies.

For an update, review upstream maintenance, release notes, licenses and security advisories, change the relevant pins, regenerate with `uv lock`, inspect the lock diff, then run the full required checks. Review the Node binary wheel and its bundled components alongside Pyright. Do not enable Pyright's automatic version override. Use `uv run ruff check --fix` and `uv run ruff format` for intentional Python fixes; CI only checks formatting. Keep dependency-update and application behavior changes separately reviewable.

## Review and integration

Describe the behavior change, reason, scope, risk and validation. Link the closing issue. Update documentation and remove superseded implementation in the same PR.

Applicable checks and independent review must pass. Resolve findings publicly. Changes requiring maintainer approval must receive that approval. Squash merge; do not push directly to main after bootstrap or rewrite shared history.

For stacked PRs, retarget dependents before merging their base and retain required branches. Distinguish implementation, review, merge and release status.

## Decisions and safety

Record material product, architecture, service, cost, security or legal decisions with options, rationale and required evidence. Supersede accepted decision records rather than rewriting them.

Never commit secrets, real environment files or private operational notes. Use placeholders in examples. Do not start unapproved live provider, paid or voice tests. Review native components as well as Python dependencies.

Follow [AGENTS.md](AGENTS.md) for coordination, worker visibility, worktree ownership and development-storage controls. Keep personal filesystem locations and session-management notes out of public issue bodies when they are not needed for collaboration.
