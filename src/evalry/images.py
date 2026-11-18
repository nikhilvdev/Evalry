"""Which runner images may run untrusted code (security review F8).

A task author names the image a repository task runs in, so without a list any public image could be pulled and run. An image
must be pinned by digest and either match an entry exactly or start with an allowed registry prefix (an entry ending in "/").
The built-in Python image is always allowed. ``EVALRY_RUNNER_IMAGES`` adds entries, comma separated, for example
``registry.internal/evalry/,node@sha256:...``. This is policy, not scanning: whoever allows an image owns its contents.
"""

import os
import re
from collections.abc import Mapping

# Pinned by digest so a moved tag cannot change scores. shortcut: Python 3.12 only, a language profile registry comes later.
PYTHON_IMAGE = "python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
PINNED = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")


def allowed_entries(env: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if env is None else env
    extra = [e.strip() for e in (env.get("EVALRY_RUNNER_IMAGES") or "").split(",") if e.strip()]
    return [PYTHON_IMAGE, *extra]


def check_image(image: str, env: Mapping[str, str] | None = None) -> None:
    """Raise ValueError, saying what to do, unless ``image`` is digest-pinned and allowed."""
    if not PINNED.fullmatch(image):
        raise ValueError(
            f"Image {image!r} is not pinned by digest (name@sha256:...). A moved tag could change scores."
        )
    for entry in allowed_entries(env):
        if image == entry or (entry.endswith("/") and image.startswith(entry)):
            return
    raise ValueError(
        f"Image {image!r} is not on the runner image allowlist. Ask the platform owner to review it and add it to "
        "EVALRY_RUNNER_IMAGES (an exact digest-pinned reference, or a registry prefix ending in '/')."
    )
