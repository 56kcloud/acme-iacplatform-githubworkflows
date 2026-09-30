# acme-iacplatform-githubworkflows

Shared reusable Terraform workflows, built from the existing
`deploy-prod.yml` and `ci.yml` templates. Only the changes listed below were
made; everything else is the template as it was.

| Workflow | From template | Job |
|---|---|---|
| `terraform-deploy.yml` | `deploy-prod.yml` | One job: plan on PR, apply on push to `main`, manual plan/apply by dispatch |
| `terraform-module-ci.yml` | `ci.yml` | One job: fmt, terraform-docs, TFLint, Trivy, Checkov, init, validate, test. No AWS, no plan or apply. |

`lint-workflows.yml` and `check-drift.yml` are not shared: each repo keeps its
own copy, as today.

## Calling them

Pin by full commit SHA with the version as a comment.

Deploy repo, one stub per environment (`.github/workflows/deploy-prod.yml`):

```yaml
name: Deploy prod

on:
  push:
    branches: [main]
    paths: ['prod/**', '.github/workflows/deploy-prod.yml']
  pull_request:
    branches: [main]
    paths: ['prod/**', '.github/workflows/deploy-prod.yml']
  workflow_dispatch:
    inputs:
      action:
        description: 'Terraform action to perform'
        required: true
        type: choice
        options: [plan, apply]

permissions:
  contents: read

jobs:
  deploy:
    uses: ACME-internal/xxx-githubworkflows/.github/workflows/terraform-deploy.yml@<sha> # v0.1.0
    permissions:
      contents: read
      id-token: write
      pull-requests: write
    with:
      deploy-env: prod
      action: ${{ inputs.action }}
      aws-account-id: "12345678912"
    secrets:
      GITHUBMCH_REPOS_READ_APP_PRIVATE_KEY: ${{ secrets.GITHUBMCH_REPOS_READ_APP_PRIVATE_KEY }}
      TFC_REGISTRY_READ_TOKEN: ${{ secrets.TFC_REGISTRY_READ_TOKEN }}
```

Module repo (`.github/workflows/ci.yml`):

```yaml
name: CI

on:
  pull_request:
    branches: [main]

permissions:
  contents: read

jobs:
  ci:
    uses: ACME-internal/xxx-githubworkflows/.github/workflows/terraform-module-ci.yml@<sha> # v0.1.0
    permissions:
      contents: read
    secrets:
      GITHUBMCH_REPOS_READ_APP_PRIVATE_KEY: ${{ secrets.GITHUBMCH_REPOS_READ_APP_PRIVATE_KEY }}
      TFC_REGISTRY_READ_TOKEN: ${{ secrets.TFC_REGISTRY_READ_TOKEN }}
```

The caller must grant the permissions; a reusable workflow can't raise them.

### terraform-deploy.yml inputs

| Input | Default | Notes |
|---|---|---|
| `deploy-env` | required | Environment directory and GitHub environment name |
| `action` | `plan` | The caller's dispatch input; ignored on push and PR |
| `aws-account-id` | `""` | Empty skips AWS credentials, for testing without AWS |
| `aws-region` | `eu-central-2` | |
| `aws-deploy-role` | `acme-github-deploy` | |

### Secrets and variables

| Name | Kind | Notes |
|---|---|---|
| `GITHUBMCH_REPOS_READ_APP_CLIENT_ID` | org variable | Read by the workflows through `vars`; stubs don't pass it |
| `GITHUBMCH_REPOS_READ_APP_PRIVATE_KEY` | secret, required | Passed explicitly by the stub |
| `TFC_REGISTRY_READ_TOKEN` | secret, optional | Only for modules still in the TF Cloud registry, which is going away |

## Vendored configs

`configs/deploy/` (for each env directory of a deploy repo) and
`configs/module/` (for the root of a module repo) hold `.tflint.hcl`,
`trivy.yaml`, `.trivyignore`, `.checkov.yml` and `.terraform-docs.yml`.
Callers commit copies, and the copies follow the SHA their stub pins. In a
deploy repo each env dir follows the SHA of its own stub
(`deploy-<env>.yml`), so a config change rolls out per env like the workflow
does. Module stubs are named `ci.yml`.

- A mise task, `config:sync`, copies them from this repo at the pinned SHA. It
  lives in each caller repo's root `mise.toml`. The task fetches
  `config_sync.py` from this repo's default branch; the script reads the
  caller's stub and re-runs itself at the pinned version.
- Both workflows run `config_sync.py check` directly, not through the caller's
  task, at `job.workflow_sha`. A drifted copy fails that step, so the check
  goes red. It ignores line endings, trailing newlines and file mode, and
  prints a diff and the command to fix it.
- A file listed in `CONFIG_SKIP` under `[_]` in the caller's `mise.toml` is
  owned by the caller and not checked. trivy's `severity` is an exact list, so
  a local `trivy.yaml` must keep `HIGH` and `CRITICAL`; give the platform team
  CODEOWNERS on these files.

### Caller tasks

Deploy repo, root `mise.toml` (tasks only; tool versions stay in each env's
`mise.toml`):

```toml
[tasks."config:sync"]
description = "Vendor configs/deploy into an env dir at the SHA its stub pins"
usage = 'arg "<env>" help="Environment directory, e.g. prod"'
shell = "bash -c"
run = """
set -euo pipefail
gh api -H 'Accept: application/vnd.github.raw' \
  repos/ACME-internal/xxx-githubworkflows/contents/scripts/config_sync.py \
  | mise x python@3.12 -- python3 - sync --env-dir "${usage_env}"
"""

[tasks."config:check"]
description = "Check an env dir's vendored configs match the SHA its stub pins, as CI does"
usage = 'arg "<env>" help="Environment directory, e.g. prod"'
shell = "bash -c"
run = """
set -euo pipefail
gh api -H 'Accept: application/vnd.github.raw' \
  repos/ACME-internal/xxx-githubworkflows/contents/scripts/config_sync.py \
  | mise x python@3.12 -- python3 - check --env-dir "${usage_env}"
"""

[tasks."workflows:pin"]
description = "Pin an env's workflow stub to a shared-repo tag, then sync its configs"
usage = '''
arg "<env>" help="Environment directory, e.g. prod"
arg "<version>" help="Tag of the shared repo, e.g. v0.1.3"
'''
shell = "bash -c"
run = """
set -euo pipefail
gh api -H 'Accept: application/vnd.github.raw' \
  repos/ACME-internal/xxx-githubworkflows/contents/scripts/config_sync.py \
  | mise x python@3.12 -- python3 - pin --env-dir "${usage_env}" --version "${usage_version}"
"""
```

Module repo: the same three tasks in the root `mise.toml`, with no `<env>`
argument and `--profile module --env-dir .` in place of `--env-dir`.

`set -euo pipefail` matters: without `pipefail`, a failed download hands
Python an empty script and the task passes having done nothing.

## Updating the pin

`mise run workflows:pin <env> <version>` resolves the tag to its commit
(branches are refused), rewrites that env's stub to `@<sha> # <tag>`, then
syncs the env's vendored configs at the new version. Commit the stub and
configs together in one PR. Pin envs one at a time (devt, then depl, then
prod) to roll an upgrade through them.

## Changes from the templates

Both workflows:
- are `workflow_call` workflows, still one job each;
- take tool versions from the caller's `mise.toml` and pin the mise version
  themselves: 2026.9.16, up from the templates' 2026.3.4, which can't install
  trivy or checkov on GitHub-hosted runners (their orgs' IP allow lists reject
  the job token with a 403). The deploy workflow runs mise in the env directory,
  so a per-env `mise.toml` wins over a root one;
- run the tools directly, not the caller's mise tasks;
- fail on `terraform fmt` and terraform-docs diffs, with no auto-fix commits.
  The deploy workflow gained a `terraform-docs . --output-check` step in the
  env directory, so each env needs a `.terraform-docs.yml`;
- take secrets explicitly, with the TF Cloud registry token optional;
- mint the GitHub App token with `permission-contents: read`, the client ID
  from the org variable, and `owner` from the calling repo's owner;
- remove the `url.insteadOf` git credentials at the end of the job, even on
  failure;
- check the caller's vendored configs against `configs/` at the pinned SHA;
- write a step summary with the result of each check;
- pass `actionlint` and `zizmor --persona=pedantic`.

Deploy only:
- the PR plan comment's header includes the env name, so each environment
  gets its own comment;
- apply runs only on `main`: a dispatched apply from any other branch fails
  in "Require main for apply", so it can't skip CODEOWNERS review;
- the AWS account, region and role are inputs, and an empty account skips
  AWS credentials.

## Known issues, left for the tooling follow-up

- **zizmor exception.** `zizmor --persona=pedantic` flags an App token with no
  `repositories:` list (`github-app`, high). The token deliberately reads
  every repo the App is installed on, so the finding is suppressed on the
  `owner:` line with the reason. The App must stay Contents: read only. The
  templates hit the same finding.
- **PR plans wait for environment reviewers.** As in the template, the job
  always binds `environment: <env>`. `deployment:` only controls whether a
  deployment record is created: required reviewers still apply, so a PR plan
  for a protected environment waits for approval. The OIDC `sub` claim is
  environment-shaped on every run, PRs included.
- **Plan and apply share one job and one role**, and apply runs with
  `-auto-approve` rather than applying the reviewed plan file.
- The `lint-workflows.yml` template runs `zizmor --persona=pedantic .ß`; the
  stray `ß` makes zizmor fail. This repo's copy has it fixed.
