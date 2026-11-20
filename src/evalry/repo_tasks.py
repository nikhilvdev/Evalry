"""Repository tasks (plan §4 tiers 3 and 4): snapshots, the model's edits, and the workspace that gets tested.

A snapshot is a deterministic tar.gz of a pinned source tree, stored in object storage by its SHA-256. Everything that enters a
workspace is validated on the host before any container sees it: only regular files and directories, only safe relative
paths, bounded sizes. The container is handed one tar stream (snapshot, then the model's edits, then the hidden tests on top)
and runs the repo's own test command; it never gets a volume or the network.
"""

import gzip
import hashlib
import io
import re
import tarfile
from collections.abc import Mapping
from pathlib import Path

from evalry.safepath import safe_relpath

MAX_SNAPSHOT_BYTES = 100 * 1024 * 1024  # compressed
MAX_UNPACKED_BYTES = 200 * 1024 * 1024
MAX_FILES = 20_000
MAX_EDIT_FILES = 100
MAX_EDIT_FILE_BYTES = 1024 * 1024
MAX_EDIT_TOTAL_BYTES = 5 * 1024 * 1024

Files = dict[str, bytes]
Edits = dict[str, str | None]  # path -> new content, or None to delete the file


class RepoError(ValueError):
    """A snapshot, edit or workspace broke a rule. The message says which and what to do."""


def _check_path(path: str) -> None:
    """``safe_relpath`` with the error type this module's callers handle."""
    try:
        safe_relpath(path)
    except ValueError as e:
        raise RepoError(str(e)) from e


def pack_snapshot(root: Path) -> bytes:
    """Deterministic tar.gz of ``root`` (same tree, same bytes, same digest). Refuses symlinks rather than guessing."""
    if not root.is_dir():
        raise RepoError(f"{root} is not a directory.")
    buf = io.BytesIO()
    total = 0
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        count = 0
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root).as_posix()
            if rel == ".git" or rel.startswith(".git/"):
                continue
            if path.is_symlink():
                raise RepoError(
                    f"{rel} is a symlink. Snapshots hold regular files only; replace it with a copy."
                )
            if path.is_dir():
                continue
            _check_path(rel)
            data = path.read_bytes()
            count += 1
            total += len(data)
            if count > MAX_FILES or total > MAX_UNPACKED_BYTES:
                raise RepoError(
                    f"Snapshot is over {MAX_FILES} files or {MAX_UNPACKED_BYTES // 2**20} MB unpacked. Trim the tree."
                )
            info = tarfile.TarInfo(rel)
            info.size, info.mtime, info.mode = len(data), 0, 0o755 if path.stat().st_mode & 0o111 else 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_snapshot(data: bytes, expected_digest: str | None = None) -> Files:
    """Validate and unpack a snapshot into memory. Raises RepoError on any member that could escape or exhaust the host."""
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise RepoError(f"Snapshot is {len(data)} bytes; the limit is {MAX_SNAPSHOT_BYTES}.")
    if expected_digest and digest(data) != expected_digest:
        raise RepoError(
            "Snapshot bytes do not match the digest the task pins. Re-upload the snapshot or fix the task."
        )
    files: Files = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for m in tar:
                if m.isdir():
                    continue
                if not m.isreg():
                    raise RepoError(
                        f"Snapshot member {m.name!r} is not a regular file (symlink, device or link)."
                    )
                _check_path(m.name)
                total += m.size
                if len(files) >= MAX_FILES or total > MAX_UNPACKED_BYTES:
                    raise RepoError("Snapshot unpacks past the file or size limit.")
                f = tar.extractfile(m)
                assert f is not None
                files[m.name] = f.read(m.size)
    except (tarfile.TarError, OSError, EOFError) as e:
        raise RepoError(f"Snapshot is not a valid tar.gz ({type(e).__name__}).") from e
    return files


def build_workspace(base: Files, *layers: Mapping[str, str | bytes | None]) -> bytes:
    """One tar.gz: ``base`` with each layer applied in order (a later layer wins; a None value deletes). Paths are re-validated."""
    merged: dict[str, bytes] = dict(base)
    for layer in layers:
        for path, content in layer.items():
            _check_path(path)
            if content is None:
                merged.pop(path, None)
            else:
                merged[path] = content.encode() if isinstance(content, str) else content
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        for path in sorted(merged):
            info = tarfile.TarInfo(path)
            info.size, info.mtime, info.mode = len(merged[path]), 0, 0o644
            tar.addfile(info, io.BytesIO(merged[path]))
    return buf.getvalue()


# -- the model's edits -------------------------------------------------------------------------------------------------

_BLOCK = re.compile(r'<file path="([^"\n]+)">\n(.*?)\n?</file>', re.DOTALL)


def extract_edits(text: str) -> dict[str, str]:
    """Parse ``<file path="...">content</file>`` blocks from a reply. Last block for a path wins. No blocks means no edits.

    Raises RepoError for an unsafe path or an oversize edit: that sample scores 0 rather than being repaired.
    """
    edits: dict[str, str] = {}
    for path, content in _BLOCK.findall(text):
        try:
            safe_relpath(path)
        except ValueError as e:
            raise RepoError(str(e)) from e
        edits[path] = content
    check_edits(edits)
    return edits


def check_edits(edits: Mapping[str, str | None]) -> None:
    if len(edits) > MAX_EDIT_FILES:
        raise RepoError(f"{len(edits)} files edited; the limit is {MAX_EDIT_FILES}.")
    total = 0
    for path, content in edits.items():
        _check_path(path)
        size = len((content or "").encode())
        total += size
        if size > MAX_EDIT_FILE_BYTES or total > MAX_EDIT_TOTAL_BYTES:
            raise RepoError(f"Edit to {path!r} pushes the reply past the size limits.")


def render_edits(edits: Edits) -> str:
    """Canonical text of an edit set: what is stored as the sample's code, so it can be re-graded exactly."""
    return "\n".join(
        f'<file path="{p}" deleted="true"></file>' if c is None else f'<file path="{p}">\n{c}\n</file>'
        for p, c in sorted(edits.items())
    )


def repo_prompt(issue: str, files: Files, *, max_chars: int = 150_000) -> str:
    """The issue plus the repository's text files (up to a budget) and the reply format. Hidden tests are not in a snapshot."""
    parts: list[str] = []
    used, omitted = 0, []
    for path in sorted(files):
        try:
            text = files[path].decode()
        except UnicodeDecodeError:
            omitted.append(path)
            continue
        block = f'<file path="{path}">\n{text}\n</file>'
        if used + len(block) > max_chars or len(text) > 30_000:
            omitted.append(path)
            continue
        parts.append(block)
        used += len(block)
    note = f"\nFiles not shown (binary or too large): {', '.join(omitted[:50])}\n" if omitted else ""
    return (
        f"Resolve this issue in the repository below.\n\n<issue>\n{issue}\n</issue>\n\n"
        "Reply with the complete new content of every file you change or add, each as\n"
        '<file path="relative/path">\ncontent\n</file>\nand nothing that is not a file block needs to change.\n\n'
        f"<repository>\n{chr(10).join(parts)}\n</repository>{note}"
    )


def workspace_edits(base: Files, final: Files) -> dict[str, str | None]:
    """What an agent changed: new or modified text files and deletions. Binary changes are not representable and are ignored."""
    edits: dict[str, str | None] = {}
    for path, data in final.items():
        if base.get(path) != data:
            try:
                edits[path] = data.decode()
            except UnicodeDecodeError:
                continue
    edits.update({p: None for p in base if p not in final})
    check_edits(edits)
    return edits
