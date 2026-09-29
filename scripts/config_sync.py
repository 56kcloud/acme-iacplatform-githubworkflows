#!/usr/bin/env python3
"""Vendor shared configs at the SHA the caller's stubs pin, or check they match.

  config_sync.py sync  --env-dir engorg
  config_sync.py check --env-dir engorg --source-dir .shared --expect-sha "$SHA"
  config_sync.py check --profile module --env-dir . --source-dir .shared --expect-sha "$SHA"
"""

import argparse
import base64
import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    sys.exit("config_sync.py needs Python >= 3.11 (try: mise x python@3.12 -- python3 ...)")

SHARED_REPO_NAME = "acme-iacplatform-githubworkflows"
STUBS = {
    "deploy": ("deploy-{env}.yml",),
    "module": ("ci.yml",),
}
USES_RE = re.compile(
    r"^\s*uses:\s*(?P<repo>[\w.-]+/" + re.escape(SHARED_REPO_NAME) + r")"
    r"/\.github/workflows/[\w.-]+@(?P<ref>\S+)"
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


class SyncError(Exception):
    def __init__(self, msg, file=None, line=None):
        super().__init__(msg)
        self.file, self.line = file, line


def error(msg, file=None, line=None):
    first, *rest = msg.splitlines()
    if IN_ACTIONS:
        loc = ",".join(f"{k}={v}" for k, v in (("file", file), ("line", line)) if v)
        print(f"::error {loc}::{first}" if loc else f"::error::{first}")
    else:
        print(f"error: {first}")
    for r in rest:
        print(r)


def pinned_ref(repo_root, env, profile):
    """Return (repo, sha) pinned by the caller's stubs, which must all agree."""
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
    for (stub, lineno), (_, ref) in found.items():
        if not SHA_RE.match(ref):
            raise SyncError(f"{stub}:{lineno} pins '@{ref}', not a 40-character commit SHA", stub, lineno)
    if len(set(found.values())) > 1:
        lines = "\n".join(f"  {s}:{n}: {r}@{ref}" for (s, n), (r, ref) in found.items())
        (stub, lineno), *_ = found
        raise SyncError(f"stubs for '{env}' pin different versions:\n{lines}", stub, lineno)
    return next(iter(found.values()))


def skip_list(repo_root, env_dir):
    """Map each CONFIG_SKIP entry to the (file, line) that lists it."""
    skip = {}
    for cfg in dict.fromkeys((repo_root / "mise.toml", env_dir / "mise.toml")):
        if cfg.exists():
            text = cfg.read_text()
            line = next((i for i, l in enumerate(text.splitlines(), 1) if l.strip().startswith("CONFIG_SKIP")), None)
            for name in tomllib.loads(text).get("_", {}).get("CONFIG_SKIP", []):
                skip[name] = (str(cfg.relative_to(repo_root)), line)
    return skip


def gh_json(path):
    out = subprocess.run(["gh", "api", path], check=True, capture_output=True, text=True)
    return json.loads(out.stdout)


def shared_files(profile, repo, sha, source_dir):
    """Map filename -> bytes for configs/<profile>/ at the pinned SHA."""
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
        raise SyncError(f"fetching configs/{profile} from {repo}@{sha[:12]} failed:\n{e.stderr.strip()}")


def normalise(data):
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

    repo_root = args.repo_root.resolve()
    env_dir = (repo_root / args.env_dir).resolve()
    env = env_dir.name
    if not env_dir.is_dir():
        raise SyncError(f"env directory {args.env_dir} does not exist")

    repo, sha = pinned_ref(repo_root, env, args.profile)
    if args.expect_sha and args.expect_sha != sha:
        raise SyncError(
            f"stubs for '{env}' pin {sha[:12]} but the running workflow is {args.expect_sha[:12]}; "
            "the stub parser and the runner disagree about which version is in use"
        )

    files = shared_files(args.profile, repo, sha, args.source_dir)
    skip = skip_list(repo_root, env_dir)
    unknown = sorted(skip.keys() - files.keys())
    if unknown:
        raise SyncError(f"CONFIG_SKIP names files the shared repo does not provide: {', '.join(unknown)}", *skip[unknown[0]])

    problems = 0
    for name, upstream in files.items():
        local = env_dir / name
        rel = local.relative_to(repo_root)
        if name in skip:
            print(f"skip     {rel} (CONFIG_SKIP)")
            continue
        want = normalise(upstream)
        if args.mode == "sync":
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
            hunk = next((re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", d) for d in diff if d.startswith("@@")), None)
            error(f"{rel} differs from {repo}@{sha[:12]}\n" + "\n".join(diff[:40]), str(rel), hunk and hunk.group(1))
        else:
            print(f"ok       {rel}")

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
    try:
        sys.exit(main())
    except SyncError as e:
        error(str(e), e.file, e.line)
        sys.exit(1)
