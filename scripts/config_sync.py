#!/usr/bin/env python3
"""Copy the shared configs into a caller repo at the SHA its stub pins, or check they match.

Run from the caller repo's root:

  config_sync.py sync  --env-dir engorg                  # deploy repo env
  config_sync.py check --env-dir engorg
  config_sync.py check --profile module --env-dir .      # module repo
"""

import argparse
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path

import tomllib

SHARED_REPO_NAME = "acme-iacplatform-githubworkflows"

# The caller workflow file that pins the shared repo. In a deploy repo each env
# has its own stub, so each env dir follows its own pin.
STUBS = {"deploy": "deploy-{env}.yml", "module": "ci.yml"}

# `uses: <owner>/<shared repo>/.github/workflows/<file>@<ref>`
USES_RE = re.compile(
    r"^\s*uses:\s*(?P<repo>[\w.-]+/" + re.escape(SHARED_REPO_NAME) + r")"
    r"/\.github/workflows/[\w.-]+@(?P<ref>\S+)"
)


def gh_api(path, raw=False):
    """Call the GitHub API through the gh CLI (GH_TOKEN in CI, the user's login locally)."""
    headers = ["-H", "Accept: application/vnd.github.raw"] if raw else []
    result = subprocess.run(["gh", "api", *headers, path], stdout=subprocess.PIPE, check=False)
    if result.returncode:
        sys.exit(f"error: gh api {path} failed")
    return result.stdout


def pinned_ref(env_dir, profile):
    """Return (repo, ref) from the stub's first `uses:` line that calls the shared repo."""
    stub = Path(".github/workflows") / STUBS[profile].format(env=env_dir.name)
    if not stub.exists():
        sys.exit(f"error: {stub} not found")
    for line in stub.read_text().splitlines():
        m = USES_RE.match(line)
        if m:
            return m["repo"], m["ref"]
    sys.exit(f"error: {stub} has no uses: line calling {SHARED_REPO_NAME}")


def shared_files(repo, ref, profile):
    """Map filename -> bytes for configs/<profile>/ at `ref`."""
    listing = json.loads(gh_api(f"repos/{repo}/contents/configs/{profile}?ref={ref}"))
    return {
        e["name"]: gh_api(f"repos/{repo}/contents/{e['path']}?ref={ref}", raw=True)
        for e in listing
        if e["type"] == "file"
    }


def config_skip(env_dir):
    """Files the caller owns: CONFIG_SKIP under [_] in the env's mise.toml (mise ignores [_])."""
    cfg = env_dir / "mise.toml"
    if not cfg.exists():
        return set()
    return set(tomllib.loads(cfg.read_text()).get("_", {}).get("CONFIG_SKIP", []))


def normalise(data):
    """LF line endings and exactly one trailing newline, so neither counts as drift."""
    data = data.replace(b"\r\n", b"\n").rstrip(b"\n")
    return data + b"\n" if data else b""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["sync", "check"])
    ap.add_argument("--env-dir", required=True, type=Path)
    ap.add_argument("--profile", default="deploy", choices=["deploy", "module"])
    args = ap.parse_args()

    repo, ref = pinned_ref(args.env_dir, args.profile)
    skip = config_skip(args.env_dir)

    # Walk the shared files only: anything else in the env dir (main.tf,
    # mise.toml, README.md) belongs to the caller and is never touched.
    # Every file is visited before failing, so one run reports all drift.
    drifted = 0
    for name, upstream in shared_files(repo, ref, args.profile).items():
        local = args.env_dir / name

        # Listed in CONFIG_SKIP: the caller owns this file, neither written nor checked.
        if name in skip:
            print(f"skip     {local}")
            continue

        # Normalise the shared copy once; both sync and check use this form.
        want = normalise(upstream)

        if args.mode == "sync":
            # Write the normalised form, so a fresh sync always passes check.
            local.write_bytes(want)
            print(f"wrote    {local}")
        elif not local.exists():
            drifted += 1
            print(f"missing  {local}")
        elif normalise(local.read_bytes()) != want:
            # Normalise the local copy too: a CRLF checkout or an extra blank
            # line at the end is not drift. Anything else is.
            drifted += 1
            print(f"differs  {local}")
            # Shared version first, so "-" lines are what the file should say
            # and "+" lines are the local changes.
            sys.stdout.writelines(difflib.unified_diff(
                want.decode(errors="replace").splitlines(keepends=True),
                normalise(local.read_bytes()).decode(errors="replace").splitlines(keepends=True),
                f"{repo}@{ref[:12]}:configs/{args.profile}/{name}", str(local),
            ))
        else:
            print(f"ok       {local}")

    if drifted:
        env_arg = "" if args.profile == "module" else f" {args.env_dir}"
        print(f"\n{drifted} file(s) differ from {repo}@{ref[:12]}. Fix: run `mise run config:sync{env_arg}` and commit.")
        return 1
    print(f"{args.mode}: {args.env_dir} matches {repo}@{ref[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
