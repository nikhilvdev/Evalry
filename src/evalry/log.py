"""Single entry point for logging; all Evalry code logs through LogQuill (see CLAUDE.md)."""

from logquill import ConsoleTransport, Level, Logger, PIIRedactPlugin, RedactPlugin

_root = Logger("evalry", level=Level.INFO, transports=[ConsoleTransport()])


def get_logger(component: str) -> Logger:
    """Return ``evalry.<component>`` with redaction on.

    LogQuill children share the parent's transports but not its plugins, so redaction must be attached to
    every child or secrets reach the transport in plain text. Bind run/tenant context with ``logquill.bind_context``.
    """
    return _root.child(component).use(RedactPlugin()).use(PIIRedactPlugin())
