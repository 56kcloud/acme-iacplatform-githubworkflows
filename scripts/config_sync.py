#!/usr/bin/env python3
r"""Vendor shared configs at the SHA the caller's stubs pin, or check they match.

Run from the caller repo's root. Locally, fetch it from the shared repo's
default branch; it re-runs itself at the version the stub pins:

  gh api -H 'Accept: application/vnd.github.raw' \
    repos/<owner>/acme-iacplatform-githubworkflows/contents/scripts/config_sync.py \
    | mise x python@3.12 -- python3 - sync --env-dir engorg

  config_sync.py sync  --env-dir engorg                    # deploy repo env
  config_sync.py check --profile module --env-dir .        # module repo
  config_sync.py check --env-dir engorg --expect-sha "$SHA" # CI, already pinned
  config_sync.py check --env-dir engorg --source-dir ../acme-iacplatform-githubworkflows
"""

import argparse
import base64
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# tomllib is stdlib only from 3.11. Fail with a hint rather than an ImportError.
try:
    import tomllib
except ModuleNotFoundError:
    sys.exit("config_sync.py needs Python >= 3.11 (try: mise x python@3.12 -- python3 ...)")

# The repo name is fixed; the owner is not, so the same script works in any org.
SHARED_REPO_NAME = "acme-iacplatform-githubworkflows"

# Which caller workflow files pin the shared repo, per profile. {env} is the
# env directory's name, so deploy repos have one stub per environment.
STUBS = {
    "deploy": ("deploy-{env}.yml",),
    "module": ("ci.yml",),
}

# Matches `uses: <owner>/<shared repo>/.github/workflows/<file>@<ref>` and
# captures owner/repo and the ref. Other `uses:` lines in the stub are ignored.
USES_RE = re.compile(
    r"^\s*uses:\s*(?P<repo>[\w.-]+/" + re.escape(SHARED_REPO_NAME) + r")"
    r"/\.github/workflows/[\w.-]+@(?P<ref>\S+)"
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# In Actions, errors are printed as ::error annotations so they show on the PR.
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


class SyncError(Exception):
    """A setup problem that stops the run. Carries a file and line for the annotation."""

    def __init__(self, msg, file=None, line=None):
        super().__init__(msg)
        self.file, self.line = file, line


def error(msg, file=None, line=None):
    """Print one error: an annotation in Actions, a plain line elsewhere.

    Only the first line becomes the annotation; any following lines (a diff)
    are printed as ordinary log output underneath. Everything goes to stdout so
    errors stay in order with the ok/skip lines.
    """
    first, *rest = msg.splitlines()
    if IN_ACTIONS:
        loc = ",".join(f"{k}={v}" for k, v in (("file", file), ("line", line)) if v)
        print(f"::error {loc}::{first}" if loc else f"::error::{first}")
    else:
        print(f"error: {first}")
    for r in rest:
        print(r)


def pinned_ref(repo_root, env, profile):
    """Return (repo, sha) pinned by the caller's stubs, which must all agree.

    The pinned SHA is the only source of truth for which configs to vendor, so
    configs move exactly when the stub is repinned. Every matching `uses:`
    line is collected, keyed by (file, line), so errors can point at the exact
    line.
    """
    names = [n.format(env=env) for n in STUBS[profile]]
    found = {}
    for name in names:
        stub = repo_root / ".github" / "workflows" / name
        if not stub.exists():
            continue
        for lineno, line in enumerate(stub.read_text().splitlines(), 1):
            m = USES_RE.match(line)
            if m:
                found[(str(stub.relative_to(repo_root)), lineno)] = (m["repo"], m["ref"])
    if not found:
        raise SyncError(
            f"no stub for '{env}' calls {SHARED_REPO_NAME} "
            f"(looked for {', '.join('.github/workflows/' + n for n in names)})"
        )
    # A tag or branch can move after review; only a full commit SHA is fixed.
    for (stub, lineno), (_, ref) in found.items():
        if not SHA_RE.match(ref):
            raise SyncError(f"{stub}:{lineno} pins '@{ref}', not a 40-character commit SHA", stub, lineno)
    # One version per env: if two lines pin different SHAs there is no single
    # answer to "which configs", so refuse rather than pick one.
    if len(set(found.values())) > 1:
        lines = "\n".join(f"  {s}:{n}: {r}@{ref}" for (s, n), (r, ref) in found.items())
        (stub, lineno), *_ = found
        raise SyncError(f"stubs for '{env}' pin different versions:\n{lines}", stub, lineno)
    return next(iter(found.values()))


def skip_list(repo_root, env_dir):
    """Map each CONFIG_SKIP entry to the (file, line) that lists it.

    CONFIG_SKIP lives under [_] in mise.toml, a table mise itself never reads.
    Both the repo root and the env directory are read; in a module repo they
    are the same file, which dict.fromkeys de-duplicates. The line of the
    CONFIG_SKIP key is kept so a bad entry can be annotated in place.
    """
    skip = {}
    for cfg in dict.fromkeys((repo_root / "mise.toml", env_dir / "mise.toml")):
        if cfg.exists():
            text = cfg.read_text()
            line = next((i for i, l in enumerate(text.splitlines(), 1) if l.strip().startswith("CONFIG_SKIP")), None)
            for name in tomllib.loads(text).get("_", {}).get("CONFIG_SKIP", []):
                skip[name] = (str(cfg.relative_to(repo_root)), line)
    return skip


def gh(*args):
    """Run `gh api ...` and return stdout bytes (GH_TOKEN in CI, the user's login locally)."""
    try:
        return subprocess.run(["gh", "api", *args], check=True, capture_output=True).stdout
    except FileNotFoundError:
        raise SyncError("needs the GitHub CLI (gh), logged in with read access to the shared repo")


def gh_json(path):
    """Call the GitHub REST API through the gh CLI and parse the JSON reply."""
    return json.loads(gh(path))


def shared_files(profile, repo, sha, source_dir):
    """Map filename -> bytes for configs/<profile>/ at the pinned SHA.

    With --source-dir, read a local checkout (the caller must have it at the
    right SHA). Otherwise fetch through the API at exactly `sha`, so nothing
    from the shared repo is checked out into the caller's workspace, where
    fmt, Trivy and Checkov would scan it.
    """
    if source_dir:
        base = source_dir / "configs" / profile
        if not base.is_dir():
            raise SyncError(f"{base} does not exist in the shared checkout")
        return {p.name: p.read_bytes() for p in sorted(base.iterdir()) if p.is_file()}
    try:
        listing = gh_json(f"repos/{repo}/contents/configs/{profile}?ref={sha}")
        return {
            e["name"]: base64.b64decode(gh_json(f"repos/{repo}/contents/{e['path']}?ref={sha}")["content"])
            for e in listing
            if e["type"] == "file"
        }
    except subprocess.CalledProcessError as e:
        raise SyncError(f"fetching configs/{profile} from {repo}@{sha[:12]} failed:\n{e.stderr.decode().strip()}")


def reexec_pinned(repo, sha):
    """Replace this process with config_sync.py from `repo` at `sha`, same arguments.

    Local runs fetch this script from the shared repo's default branch, which
    can be newer than the version the stub pins. Re-running the pinned copy
    keeps the checking logic in step with the pinned configs. CONFIG_SYNC_SHA
    marks the re-run so it doesn't fetch itself again.
    """
    try:
        script = gh("-H", "Accept: application/vnd.github.raw", f"repos/{repo}/contents/scripts/config_sync.py?ref={sha}")
    except subprocess.CalledProcessError as e:
        raise SyncError(f"fetching scripts/config_sync.py from {repo}@{sha[:12]} failed:\n{e.stderr.decode().strip()}")
    path = Path(tempfile.gettempdir()) / f"config_sync-{sha[:12]}.py"
    path.write_bytes(script)
    os.environ["CONFIG_SYNC_SHA"] = sha
    # execv replaces the process, dropping anything still in the stdout buffer.
    sys.stdout.flush()
    os.execv(sys.executable, [sys.executable, str(path), *sys.argv[1:]])


def normalise(data):
    """Canonical bytes for comparison: LF line endings, exactly one trailing newline.

    A CRLF checkout or an extra blank line at the end is not drift. An empty
    file stays empty. File mode is never compared.
    """
    data = data.replace(b"\r\n", b"\n").rstrip(b"\n")
    return data + b"\n" if data else b""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["sync", "check"])
    ap.add_argument("--env-dir", required=True, type=Path)
    ap.add_argument("--profile", default="deploy", choices=["deploy", "module"])
    ap.add_argument("--repo-root", default=Path("."), type=Path)
    ap.add_argument("--source-dir", type=Path, help="shared repo checkout at the pinned SHA; default fetches with gh")
    ap.add_argument("--expect-sha", help="fail unless the stubs pin this SHA (CI passes job.workflow_sha)")
    args = ap.parse_args()

    # The env directory's name is the env name, used to find deploy-<env>.yml.
    repo_root = args.repo_root.resolve()
    env_dir = (repo_root / args.env_dir).resolve()
    env = env_dir.name
    if not env_dir.is_dir():
        raise SyncError(f"env directory {args.env_dir} does not exist")

    # In CI, job.workflow_sha is what GitHub is actually running. If the stub
    # parse disagrees, the parser is wrong, and the check must not pass on it.
    repo, sha = pinned_ref(repo_root, env, args.profile)
    if args.expect_sha and args.expect_sha != sha:
        raise SyncError(
            f"stubs for '{env}' pin {sha[:12]} but the running workflow is {args.expect_sha[:12]}; "
            "the stub parser and the runner disagree about which version is in use"
        )

    # CI passes --expect-sha and already runs the pinned copy; --source-dir
    # means the caller chose the version. Otherwise make sure the pinned copy
    # of this script is the one doing the work.
    if not args.expect_sha and not args.source_dir and os.environ.get("CONFIG_SYNC_SHA") != sha:
        reexec_pinned(repo, sha)

    files = shared_files(args.profile, repo, sha, args.source_dir)

    # A skip entry that matches nothing is almost always a typo (trivy.yml for
    # trivy.yaml); failing stops it silently skipping nothing.
    skip = skip_list(repo_root, env_dir)
    unknown = sorted(skip.keys() - files.keys())
    if unknown:
        raise SyncError(f"CONFIG_SKIP names files the shared repo does not provide: {', '.join(unknown)}", *skip[unknown[0]])

    # Walk the shared set only. Files that exist locally but not upstream
    # (main.tf, mise.toml, a README) are the caller's and are ignored. Each file
    # is all-or-nothing: vendored verbatim, or skipped and owned locally.
    problems = 0
    for name, upstream in files.items():
        local = env_dir / name
        rel = local.relative_to(repo_root)
        if name in skip:
            print(f"skip     {rel} (CONFIG_SKIP)")
            continue
        want = normalise(upstream)
        if args.mode == "sync":
            # Write the normalised form, so a fresh sync always passes check.
            local.write_bytes(want)
            local.chmod(0o644)
            print(f"wrote    {rel}")
        elif not local.exists():
            problems += 1
            error(f"{rel} missing (vendored from {repo}@{sha[:12]})", str(rel))
        elif normalise(local.read_bytes()) != want:
            problems += 1
            diff = list(difflib.unified_diff(
                want.decode(errors="replace").splitlines(),
                normalise(local.read_bytes()).decode(errors="replace").splitlines(),
                f"{repo}@{sha[:12]}:configs/{args.profile}/{name}", str(rel), lineterm="", n=1,
            ))
            # The first hunk header's "+N" is the first changed line in the
            # local file: annotate there so it lands on the PR diff.
            hunk = next((re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", d) for d in diff if d.startswith("@@")), None)
            error(f"{rel} differs from {repo}@{sha[:12]}\n" + "\n".join(diff[:40]), str(rel), hunk and hunk.group(1))
        else:
            print(f"ok       {rel}")

    # Report every mismatch before failing, with both ways out. A module repo
    # vendors to its root, so the hint drops the env argument there.
    if problems:
        is_root = env_dir == repo_root
        task = "mise run config:sync" + ("" if is_root else f" {env}")
        mise_toml = "mise.toml" if is_root else f"{env}/mise.toml"
        print(
            f"\n{problems} vendored config file(s) do not match the version the '{env}' stubs pin.\n"
            f"Fix: run `{task}` and commit the result.\n"
            f"To own a file locally instead, add it to CONFIG_SKIP under [_] in {mise_toml}."
        )
        return 1
    print(f"{args.mode}: {env} matches {repo}@{sha[:12]} ({len(files) - len(skip.keys() & files.keys())} files, {len(skip)} skipped)")
    return 0


if __name__ == "__main__":
    # Setup problems (bad stub, bad CONFIG_SKIP, fetch failure) raise SyncError
    # and exit 1 with one annotated error; file mismatches exit 1 from main().
    try:
        sys.exit(main())
    except SyncError as e:
        error(str(e), e.file, e.line)
        sys.exit(1)
