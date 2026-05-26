"""
Runner-side helpers for per-session text logs + JSONL event streams.

Generic across runners — every CLI script that launches an agent calls
``init_session_logging`` once at startup to set up:

  * A FileHandler that captures every log line at INFO+ into
    ``logs/{session_id}.log``  (human-readable, mirrors the console)
  * A ``SessionRecorder`` writing structured events to
    ``logs/{session_id}.jsonl``

Both files share the same ``session_id`` so they can be paired.

This helper lives at the repo root (not in ``ibkr/``) because it ties
together logging-config conventions with the runner CLI surface. The
package itself stays library-grade.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from ibkr.recorder import SessionRecorder


def make_session_id(prefix: str) -> str:
    """``prefix-YYYY-MM-DDTHH-MM-SS`` — filesystem-safe."""
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    return f"{prefix}-{ts}"


def init_session_logging(
    log_dir: str = "logs",
    session_id: str | None = None,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
) -> tuple[SessionRecorder, Path]:
    """
    Configure root logging (console + file) and return a SessionRecorder.

    Args:
        log_dir:       Directory to write logs into; created if missing.
        session_id:    Optional explicit session ID; auto-generated if None.
        console_level: Level for the console handler (default INFO).
        file_level:    Level for the file handler (default DEBUG — captures
                       everything for post-mortem analysis).

    Returns:
        (recorder, log_file_path) — share the path with the user if you want
        them to send the logs back for review.
    """
    sid = session_id or make_session_id("session")

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    text_log = log_path / f"{sid}.log"

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    root = logging.getLogger()
    # Capture everything; per-handler levels filter what's actually written.
    root.setLevel(logging.DEBUG)

    # Console
    if not any(isinstance(h, logging.StreamHandler)
               and not isinstance(h, logging.FileHandler)
               for h in root.handlers):
        ch = logging.StreamHandler()
        ch.setLevel(console_level)
        ch.setFormatter(formatter)
        root.addHandler(ch)
    else:
        # Already configured (e.g. from basicConfig) — just adjust its level
        for h in root.handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setLevel(console_level)

    # File handler dedicated to this session
    fh = logging.FileHandler(text_log, mode="a", encoding="utf-8")
    fh.setLevel(file_level)
    fh.setFormatter(formatter)
    root.addHandler(fh)

    recorder = SessionRecorder(log_dir, sid)

    log = logging.getLogger("session")
    log.info(f"Session log: {text_log}")
    log.info(f"Event stream: {recorder.path}")

    return recorder, text_log
