# Repository settings

The settings of `slackysoba/sobafm`, each with its reason and the `gh` command that applies it. Changes to these settings need the maintainer's approval and are made by updating this document in the same pull request.

Shell command examples use Bash, or Git Bash on Windows; their multiline continuations are not PowerShell syntax.

## Repository

| Setting | Value | Why |
| --- | --- | --- |
| Visibility | Public | SobaFM is open source |
| Features | Issues and Projects on; wiki and discussions off | Work is tracked in issues and the Project; documentation lives in `docs/` |
| Merge methods | Squash only, using the pull request title and description | One commit per issue on `main`, with the reviewed description as its message |
| Head branches | Deleted on merge; update-branch suggestions on | Keeps branches short-lived; GitHub retargets pull requests based on a branch it deletes |

```sh
gh repo edit slackysoba/sobafm --enable-issues --enable-projects --enable-wiki=false \
  --enable-squash-merge --enable-merge-commit=false --enable-rebase-merge=false \
  --squash-merge-commit-message pr-title-description \
  --delete-branch-on-merge --allow-update-branch
gh repo view slackysoba/sobafm --json visibility,hasIssuesEnabled,hasProjectsEnabled,hasWikiEnabled,squashMergeAllowed,mergeCommitAllowed,rebaseMergeAllowed,deleteBranchOnMerge
```

## Security

| Setting | Value | Why |
| --- | --- | --- |
| Secret scanning and push protection | On | Blocks pushes that contain known credential formats |
| Dependabot alerts and security updates | On | Reports vulnerable dependencies and proposes fixes |
| Private vulnerability reporting | On | The reporting channel in [SECURITY.md](../SECURITY.md) |
| CodeQL default setup | On, default query suite | Static analysis of the Python code and the workflows |

```sh
gh api -X PATCH repos/slackysoba/sobafm \
  -F 'security_and_analysis[secret_scanning][status]=enabled' \
  -F 'security_and_analysis[secret_scanning_push_protection][status]=enabled'
gh api -X PUT repos/slackysoba/sobafm/vulnerability-alerts
gh api -X PUT repos/slackysoba/sobafm/automated-security-fixes
gh api -X PUT repos/slackysoba/sobafm/private-vulnerability-reporting
gh api -X PATCH repos/slackysoba/sobafm/code-scanning/default-setup -f state=configured -f query_suite=default

gh api repos/slackysoba/sobafm --jq .security_and_analysis
gh api repos/slackysoba/sobafm/private-vulnerability-reporting
gh api repos/slackysoba/sobafm/code-scanning/default-setup --jq '{state, languages}'
```

## Ruleset on `main`

| Rule | Why |
| --- | --- |
| Pull request required, with no required approvals | Every change is reviewable; the project has a single maintainer, who cannot approve their own pull requests |
| Squash merge only, linear history | Matches the repository's merge method |
| Required checks `ci` and `security` | The single gate job of each workflow; new jobs need no ruleset change |
| Code scanning: CodeQL, no high-severity security alerts or errors | Merges cannot introduce serious findings |
| No force pushes or deletion, no bypass actors | `main` keeps its history for everyone, including administrators |

The ruleset is stored as JSON; `integration_id` 15368 is GitHub Actions.

```json
{
  "name": "main",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "bypass_actors": [],
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    { "type": "required_linear_history" },
    {
      "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": false,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": false,
        "allowed_merge_methods": ["squash"]
      }
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "do_not_enforce_on_create": false,
        "required_status_checks": [
          { "context": "ci", "integration_id": 15368 },
          { "context": "security", "integration_id": 15368 }
        ]
      }
    },
    {
      "type": "code_scanning",
      "parameters": {
        "code_scanning_tools": [
          { "tool": "CodeQL", "security_alerts_threshold": "high_or_higher", "alerts_threshold": "errors" }
        ]
      }
    }
  ]
}
```

Save the JSON as `ruleset.json`, then apply and read it back:

```sh
gh api repos/slackysoba/sobafm/rulesets --jq '.[] | {id, name}'
gh api -X PUT repos/slackysoba/sobafm/rulesets/<id> --input ruleset.json
gh api repos/slackysoba/sobafm/rules/branches/main --jq '.[].type'
```

A direct push to `main` fails with `GH013: Repository rule violations`.

## Release tag rulesets

[ADR-0005](decisions/0005-release-images-with-github-actions-and-artifact-attestations.md) requires two active tag rulesets before the first release tag. The maintainer applies them; the workflow does not change repository settings. Their [rules layer](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets#about-rule-layering), so the creation bypass does not bypass the separate update/deletion restriction.

Save this as `release-tag-creation.json`. `302669505` is the public GitHub account ID of `slackysoba`; confirm it with `gh api users/slackysoba --jq .id` before applying. The [User bypass](https://docs.github.com/en/rest/repos/rules#create-a-repository-ruleset) names that maintainer alone, rather than granting creation to every repository administrator.

```json
{
  "name": "release-tag-creation",
  "target": "tag",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["refs/tags/v*"], "exclude": [] } },
  "bypass_actors": [
    { "actor_id": 302669505, "actor_type": "User", "bypass_mode": "always" }
  ],
  "rules": [ { "type": "creation" } ]
}
```

Save this separately as `release-tag-immutability.json`. Its empty bypass list prevents everyone, including the maintainer and the release workflow, from moving or deleting a published tag while the ruleset is active.

```json
{
  "name": "release-tag-immutability",
  "target": "tag",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["refs/tags/v*"], "exclude": [] } },
  "bypass_actors": [],
  "rules": [ { "type": "update" }, { "type": "deletion" } ]
}
```

Apply both once before pushing any `v*` tag, then read each full definition back. The explicit API version supports the individual User bypass. Replace the readback IDs with those returned by creation; for an existing ruleset, review its current definition and use `PUT` with its ID instead of creating a duplicate.

```sh
gh api -X POST repos/slackysoba/sobafm/rulesets \
  -H 'X-GitHub-Api-Version: 2026-03-10' --input release-tag-creation.json
gh api -X POST repos/slackysoba/sobafm/rulesets \
  -H 'X-GitHub-Api-Version: 2026-03-10' --input release-tag-immutability.json
gh api repos/slackysoba/sobafm/rulesets --jq '.[] | {id, name, target, enforcement}'
gh api repos/slackysoba/sobafm/rulesets/CREATION_ID \
  -H 'X-GitHub-Api-Version: 2026-03-10' \
  --jq '{name, target, enforcement, conditions, bypass_actors, rules}'
gh api repos/slackysoba/sobafm/rulesets/IMMUTABILITY_ID \
  -H 'X-GitHub-Api-Version: 2026-03-10' \
  --jq '{name, target, enforcement, conditions, bypass_actors, rules}'
```

Confirm both are active and target `refs/tags/v*`: creation has only the maintainer User bypass; immutability has update and deletion restrictions and no bypass actors. Tags cannot be corrected by moving them: fix the source/version through a pull request and create a new release tag.

## Container package and release permissions

The release job alone receives `contents: write` for GitHub releases, `packages: write` for GHCR publication, `id-token: write` for OIDC signing, and `attestations: write` for storing attestations. Other jobs retain read access. `actions/attest` disables optional artifact metadata storage records, so `artifact-metadata: write` is not needed. Actions are pinned and updated by Dependabot.

GHCR initially makes a [new package private](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry), even for a public repository. After the first successful prerelease publication, the maintainer opens the `sobafm` package's **Package settings**, checks its link to `slackysoba/sobafm`, and changes **Change visibility** to **Public**. [Public visibility cannot be reverted to private](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility#configuring-visibility-of-packages-for-your-personal-account). This one-time settings action is separate from publishing the image, and needs the maintainer.

Verify public access with a newly created, empty Docker configuration directory. Copy the verified index digest from the release workflow summary; these pulls request both architectures without running either image or using saved registry credentials:

```sh
mkdir anonymous-docker-config
docker --config anonymous-docker-config pull --platform linux/amd64 \
  ghcr.io/slackysoba/sobafm@sha256:VERIFIED_INDEX_DIGEST
docker --config anonymous-docker-config pull --platform linux/arm64 \
  ghcr.io/slackysoba/sobafm@sha256:VERIFIED_INDEX_DIGEST
```

Record the package visibility, anonymous pulls, and [image attestation verification](self-hosting.md#verify-the-image-you-pulled) on #104. Publishing alone does not complete that issue's prerelease acceptance check.

## Project automation

The [SobaFM project](https://github.com/users/slackysoba/projects/2)'s built-in workflows have no API, so they are set in the Project's **Workflows** menu:

| Workflow | Setting |
| --- | --- |
| Auto-add to project | Issues from `slackysoba/sobafm` (`is:issue`) |
| Auto-add sub-issues to project | On |
| Item added to project | Issues: Status Backlog |
| Item reopened | Status Backlog |
| Pull request linked to issue | Status In review |
| Item closed | Status Done |

Pull requests are not Project items: an issue's card shows its linked pull requests. List the enabled workflows with:

```sh
gh api graphql -f query='query { user(login: "slackysoba") { projectV2(number: 2) { workflows(first: 20) { nodes { name enabled } } } } }'
```
