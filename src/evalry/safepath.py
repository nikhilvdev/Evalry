"""One definition of a safe relative path, shared by the schema, the edit parser, snapshot packing and the agent tools.

Anything that came from a model, a task author or a tarball is untrusted: a path must stay inside the workspace, so no
absolute paths, no ``..``, no backslashes or NULs, no empty parts, and not a VCS directory.
"""

MAX_PATH = 300


def safe_relpath(path: str) -> str:
    """Return ``path`` unchanged if it is safe, else raise ValueError saying why."""
    if not path or len(path) > MAX_PATH:
        raise ValueError(f"A path must be 1 to {MAX_PATH} characters.")
    if "\x00" in path or "\\" in path:
        raise ValueError(f"Path {path!r} has a NUL or backslash.")
    if path.startswith("/"):
        raise ValueError(f"Path {path!r} is absolute; use a path relative to the repository root.")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"Path {path!r} has an empty, '.' or '..' part; it must stay inside the repository.")
    if parts[0] == ".git":
        raise ValueError(f"Path {path!r} is inside .git, which is not part of a snapshot.")
    return path
