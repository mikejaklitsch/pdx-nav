"""Helpers for working with bare git repos (vanilla-tracker pattern)."""

import subprocess
import difflib
import io
import tarfile
from pathlib import Path


def git(repo, *args, timeout=30):
    """Run a git command against a bare repo, return stdout or empty string."""
    try:
        r = subprocess.run(
            ["git", f"--git-dir={repo}"] + list(args),
            capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def git_tags(repo):
    """Return {tag: short_hash} dict."""
    out = git(repo, "tag", "-l",
              "--format=%(refname:short) %(objectname:short)")
    tags = {}
    for line in out.strip().split("\n"):
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            tags[parts[0]] = parts[1]
    return tags


def git_latest_tag(repo):
    """Return the most recently created tag, or None."""
    out = git(repo, "tag", "--sort=-creatordate")
    for line in out.strip().split("\n"):
        if line.strip():
            return line.strip()
    return None


def git_resolve_ref(repo, ref):
    """Resolve a ref to a commit hash, or None."""
    out = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    return out.strip() or None


def git_resolve_version(repo, version):
    """Resolve a version string to a valid ref. Returns the version if it
    resolves as a tag or commit, None with a printed hint otherwise."""
    import sys
    tags = git_tags(repo)
    if version in tags:
        return version
    if git_resolve_ref(repo, version):
        return version
    close = difflib.get_close_matches(version, list(tags.keys()), n=3, cutoff=0.5)
    hint = (f"; did you mean: {', '.join(close)}" if close
            else f"; available: {', '.join(sorted(tags.keys()))}")
    print(f"Error: unknown version '{version}'{hint}", file=sys.stderr)
    return None


def git_archive(repo, commit, paths=None, timeout=60):
    """Return tar bytes for paths at commit. Falls back to per-path archives
    if the batched call fails."""
    cmd = ["git", f"--git-dir={repo}", "archive", "--format=tar", commit]
    if paths:
        cmd += ["--"] + list(paths)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return b""
    if r.returncode == 0:
        return r.stdout
    if not paths:
        return b""
    out = b""
    for p in paths:
        cmd2 = ["git", f"--git-dir={repo}",
                "archive", "--format=tar", commit, "--", p]
        try:
            r2 = subprocess.run(cmd2, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            continue
        if r2.returncode == 0:
            out += r2.stdout
    return out


def git_extract_files(repo, commit, paths=None, timeout=120):
    """Extract file contents from a commit via git archive.
    Returns {relative_path: text_content} with BOM stripped."""
    from .fileio import strip_bom_bytes
    tar_bytes = git_archive(repo, commit, paths, timeout)
    if not tar_bytes:
        return {}
    contents = {}
    try:
        tf = tarfile.open(fileobj=io.BytesIO(tar_bytes))
        for member in tf.getmembers():
            if not member.isfile():
                continue
            raw = tf.extractfile(member).read()
            contents[member.name] = strip_bom_bytes(raw)
        tf.close()
    except Exception:
        pass
    return contents


def git_read_file(repo, ref, rel):
    """Read a single file from a ref, stripping BOM. Returns str or None."""
    from .fileio import strip_bom_bytes
    try:
        r = subprocess.run(
            ["git", f"--git-dir={repo}", "show", f"{ref}:{rel}"],
            capture_output=True, timeout=30)
        if r.returncode != 0:
            return None
        return strip_bom_bytes(r.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
