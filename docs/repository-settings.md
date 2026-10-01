# Repository settings

The settings of `slackysoba/sobafm`, each with its reason and the `gh` command that applies it. Changes to these settings need the maintainer's approval and are made by updating this document in the same pull request.

## Repository

| Setting | Value | Why |
| --- | --- | --- |
| Visibility | Public | SobaFM is open source |
| Features | Issues and Projects on; wiki and discussions off | Work is tracked in issues and the Project; documentation lives in `docs/` |
| Merge methods | Squash only, using the pull request title and description | One commit per issue on `main`, with the reviewed description as its message |
| Head branches | Deleted on merge; update-branch suggestions on | Keeps branches short-lived |

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
