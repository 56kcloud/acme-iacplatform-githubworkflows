# acme-iacplatform-githubworkflows

Shared reusable Terraform workflows for the foundation team. Pre-1.0 sandbox
version: no AWS, local backend. Exercised by
[acme-iacsandbox-deploy](https://github.com/56kcloud/acme-iacsandbox-deploy).

## Layout

| path | what |
|---|---|
| `.github/workflows/terraform-plan.yml` | `plan` job of a caller's `terraform-<env>.yml`. Every trigger: plan, then upload the plan artifact. PRs only: scans, vendored config check. No PR comment yet. No `environment:`. |
| `.github/workflows/terraform-deploy.yml` | `apply` job: main only; `environment:` = working directory. Applies the plan job's artifact after approval, with `init -lockfile=readonly`. Never plans again. |
| `.github/workflows/terraform-module-ci.yml` | Module repos: per module fmt, validate, `terraform test`, tflint, terraform-docs check, trivy, config check. No AWS. |
| `.github/actions/*` | Composites, used from a checkout of this repo at `job.workflow_sha`. |
| `configs/deploy/` | Vendored into deploy repo env dirs by `mise run config:sync <env>`. |
| `configs/module/` | Vendored into module repo roots by `mise run config:sync`. Adds `.terraform-docs.yml`. |
| `scripts/config_sync.py` | Sync and check logic, run locally and in CI. |

## Things that are not obvious from the YAML

- **Self-checkout.** Each reusable workflow checks this repo out at
  `job.workflow_sha` into `.shared/` and calls `./.shared/.github/actions/...`.
  Inside a reusable workflow, `./.github/actions/...` resolves against the
  caller's checkout, and a cross-repo `@ref` can't be the file's own SHA.
  This needs a GitHub App token, so the App-token step is inline in the
  workflow, not in a composite.
- **App token scope = App installation.** The App is installed on all repos
  with Contents: read, and tokens carry no `repositories:` list, so private
  module sources need no per-caller configuration. Keep the App's permissions
  at Contents: read; widening them widens every job.
- **Private module sources.** Git gets the token through `GIT_CONFIG_*` env
  on `terraform init` only. Nothing is written to `~/.gitconfig`, which
  replaces the `insteadOf --global` + `if: always()` cleanup pattern.
- **`CONFIG_SKIP` hands a file to the caller.** CI then uses the caller's
  copy unchecked. trivy's `severity` is an exact list, so a local copy that
  swaps `HIGH` for `MEDIUM` hides every HIGH finding. Callers should give the
  platform team CODEOWNERS on their `mise.toml` and scanner configs.
- **Caller must grant permissions.** A reusable workflow can only narrow
  them. A caller that grants less fails before any job starts.
- **Scans gate PRs, not applies.** Enforce the plan job as a required status
  check. On `main` the plan job skips scans, so a new scanner rule can't block
  an apply.
- **Plan artifact carries the lock file.** Without it the apply job can
  resolve newer providers, and Terraform rejects the plan.
- **One role today.** `aws-role-arn` is an input on both workflows; the
  planned plan/apply role split changes the ARNs callers pass, not the
  workflows.
- **`cancel-in-progress: false`** is load-bearing: the default is `true`.
- **The trust policy is the boundary.** The main-only guard and input
  validation are tripwires for miswired callers. Nothing here validates the
  role ARN or directory a caller passes.
