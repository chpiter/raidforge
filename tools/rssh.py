#!/usr/bin/env python3
"""Minimal password-based SSH helper used to debug the live-build host.

This is a *development* convenience, not part of the ISO.  It exists so the
deployment can be rehearsed from a machine that has no ``sshpass``:

    python tools/rssh.py exec "uname -a"
    python tools/rssh.py exec --sudo "apt-get update"
    python tools/rssh.py put  app/requirements.txt /tmp/requirements.txt
    python tools/rssh.py push app /opt/raidforge --exclude __pycache__

Connection settings are read from ``tools/.remote.env`` (git-ignored):

    REMOTE_HOST=192.168.40.132
    REMOTE_USER=root
    REMOTE_PASS=...

Exit code mirrors the remote command's exit status so shell chaining works.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import posixpath
import stat
import sys
import time
from pathlib import Path
from typing import Iterable, List, Sequence

import paramiko

ENV_FILE = Path(__file__).with_name(".remote.env")


# --------------------------------------------------------------------------- #
# connection
# --------------------------------------------------------------------------- #
def load_env() -> dict:
    """Read ``tools/.remote.env`` (KEY=VALUE lines) with os.environ fallback."""
    data = {}
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip()
    for key in ("REMOTE_HOST", "REMOTE_USER", "REMOTE_PASS", "REMOTE_PORT"):
        if os.environ.get(key):
            data[key] = os.environ[key]
    missing = [k for k in ("REMOTE_HOST", "REMOTE_USER") if not data.get(k)]
    if missing:
        sys.exit(f"missing {', '.join(missing)} in {ENV_FILE}")
    return data


def connect() -> paramiko.SSHClient:
    """Open an SSH client.  Host keys are auto-added (lab network only)."""
    env = load_env()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    port = int(env.get("REMOTE_PORT") or 22)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            client.connect(
                hostname=env["REMOTE_HOST"],
                port=port,
                username=env["REMOTE_USER"],
                password=env.get("REMOTE_PASS") or None,
                look_for_keys=False,
                allow_agent=False,
                timeout=15,
                banner_timeout=20,
                auth_timeout=20,
            )
            return client
        except paramiko.AuthenticationException:
            sys.exit("authentication failed - check REMOTE_USER / REMOTE_PASS")
        except Exception as exc:  # noqa: BLE001 - transient network errors
            last_error = exc
            time.sleep(1.5)
    sys.exit(f"cannot connect to {env['REMOTE_HOST']}:{port}: {last_error}")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def run(client: paramiko.SSHClient, command: str, want_pty: bool = False) -> int:
    """Stream a remote command's stdout/stderr to our own streams."""
    stdin, stdout, stderr = client.exec_command(command, get_pty=want_pty)
    stdin.close()

    channel = stdout.channel
    exit_status = None
    try:
        while True:
            wrote = False
            if channel.recv_ready():
                chunk = channel.recv(65536)
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                wrote = True
            if channel.recv_stderr_ready():
                chunk = channel.recv_stderr(65536)
                sys.stderr.buffer.write(chunk)
                sys.stderr.buffer.flush()
                wrote = True
            if not wrote:
                if channel.exit_status_ready():
                    break
                time.sleep(0.03)
        # Drain whatever is buffered after the exit status appears.
        while channel.recv_ready():
            sys.stdout.buffer.write(channel.recv(65536))
        while channel.recv_stderr_ready():
            sys.stderr.buffer.write(channel.recv_stderr(65536))
        exit_status = channel.recv_exit_status()
    finally:
        channel.close()
        sys.stdout.buffer.flush()
        sys.stderr.buffer.flush()
    return int(exit_status if exit_status is not None else 0)


# --------------------------------------------------------------------------- #
# transfers
# --------------------------------------------------------------------------- #
def _sftp_makedirs(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    parts: List[str] = []
    current = remote_dir
    while current not in ("/", ""):
        parts.append(current)
        current = posixpath.dirname(current.rstrip("/"))
    for path in reversed(parts):
        try:
            sftp.stat(path)
        except FileNotFoundError:
            sftp.mkdir(path)


def _match(rel: str, name: str, patterns: Sequence[str]) -> bool:
    """True when a path or its basename matches one of the exclude globs."""
    return any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat) for pat in patterns)


def _walk_local(root: Path, exclude: Sequence[str]) -> Iterable[tuple]:
    """Yield ``(path, rel)`` for regular files, pruning excluded directories.

    Directory pruning matters: filtering after ``rglob`` still walks the whole
    tree, so a local ``cache/`` (700 MB of .debs) would be traversed on every
    sync even when every single file is excluded.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        rel_dir = current.relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = sorted(
            name for name in dirnames if not _match(posixpath.join(rel_dir, name), name, exclude)
        )
        for name in sorted(filenames):
            rel = posixpath.join(rel_dir, name)
            if _match(rel, name, exclude):
                continue
            path = current / name
            if path.is_symlink():
                continue
            yield path, rel


def put(client: paramiko.SSHClient, local: Path, remote: str) -> None:
    """Upload a single file, creating parent directories as needed."""
    sftp = client.open_sftp()
    try:
        _sftp_makedirs(sftp, posixpath.dirname(remote.rstrip("/")) or "/")
        sftp.put(str(local), remote)
        size = sftp.stat(remote).st_size
    finally:
        sftp.close()
    print(f"put {local} -> {remote} ({size} bytes)", file=sys.stderr)


DEFAULT_EXCLUDES = ("__pycache__", "*.pyc", ".pytest_cache", ".venv", "venv", ".DS_Store")


def push(
    client: paramiko.SSHClient,
    local: Path,
    remote_dir: str,
    exclude: Sequence[str],
    skip_unchanged: bool = True,
    dry_run: bool = False,
) -> tuple:
    """Recursively upload a directory, skipping ``exclude`` glob patterns.

    Unchanged files are skipped by comparing size + mtime.  ``put`` sets the
    remote mtime back to the local one, so the comparison is immune to clock
    skew between the two hosts; without that a remote clock running ahead of
    ours would make later edits look "already uploaded".

    With ``dry_run`` nothing is written - the would-be uploads are listed.
    """
    patterns = list(exclude) + list(DEFAULT_EXCLUDES)
    sftp = client.open_sftp()
    uploaded = skipped = 0
    try:
        _sftp_makedirs(sftp, remote_dir)
        for path, rel in _walk_local(local, patterns):
            target = posixpath.join(remote_dir, rel)
            _sftp_makedirs(sftp, posixpath.dirname(target))
            local_stat = path.stat()
            if skip_unchanged:
                try:
                    remote_stat = sftp.stat(target)
                except FileNotFoundError:
                    remote_stat = None
                if (
                    remote_stat is not None
                    and remote_stat.st_size == local_stat.st_size
                    and int(remote_stat.st_mtime) == int(local_stat.st_mtime)
                ):
                    skipped += 1
                    continue
            if dry_run:
                print(f"would push {rel}", file=sys.stdout)
                uploaded += 1
                continue
            sftp.put(str(path), target)
            sftp.utime(target, (int(local_stat.st_atime), int(local_stat.st_mtime)))
            sftp.chmod(target, 0o755 if local_stat.st_mode & stat.S_IXUSR else 0o644)
            uploaded += 1
    finally:
        sftp.close()
    verb = "to push" if dry_run else "pushed"
    print(f"{verb} {uploaded} files ({skipped} unchanged) {local} -> {remote_dir}", file=sys.stderr)
    return uploaded, skipped


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="password-based SSH helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_exec = sub.add_parser("exec", help="run a shell command remotely")
    p_exec.add_argument("command", nargs=argparse.REMAINDER)
    p_exec.add_argument("--pty", action="store_true", help="allocate a pty (login shells)")

    p_put = sub.add_parser("put", help="upload one file")
    p_put.add_argument("local")
    p_put.add_argument("remote")

    p_push = sub.add_parser("push", help="upload a directory tree")
    p_push.add_argument("local")
    p_push.add_argument("remote")
    p_push.add_argument("--exclude", action="append", default=[])
    p_push.add_argument("--no-skip", action="store_true", help="re-upload unchanged files too")
    p_push.add_argument("--dry-run", action="store_true", help="list uploads without writing")

    args = parser.parse_args(argv)
    client = connect()
    try:
        if args.cmd == "exec":
            command = " ".join(args.command).strip()
            if not command:
                sys.exit("empty command")
            return run(client, command, want_pty=args.pty)
        if args.cmd == "put":
            put(client, Path(args.local), args.remote)
            return 0
        if args.cmd == "push":
            push(
                client,
                Path(args.local),
                args.remote,
                args.exclude,
                skip_unchanged=not args.no_skip,
                dry_run=args.dry_run,
            )
            return 0
    finally:
        client.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
