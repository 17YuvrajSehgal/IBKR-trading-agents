"""
Append-only structured session recorder.

Writes one JSON object per line (JSONL) to a file in the configured directory.
Every event has:
  * `t`     — UTC ISO timestamp with millisecond precision
  * `kind`  — string discriminator chosen by the caller (e.g. "decision")
  * plus arbitrary keyword fields the caller passes in

The recorder is intentionally generic — it doesn't know about strategies,
contracts, or broker mechanics. Callers decide what events to emit and what
to put in them.

JSONL is trivial to grep/aggregate offline:
    jq -c 'select(.kind=="decision")' logs/session-*.jsonl

Thread-safe via an internal lock so it can be called from sync ib_async
event handlers and async coroutines on the same loop.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SessionRecorder:
    """
    Append-only JSONL event recorder.

    Example:
        rec = SessionRecorder("logs", session_id="multi-2026-05-26T1108")
        rec.event("connect", host="127.0.0.1", port=7497, client_id=4)
        rec.event("decision", symbol="MU", action="ENTER_LONG", z=2.3)
        rec.close()

    File layout:
        logs/{session_id}.jsonl
    """

    def __init__(
        self,
        log_dir: str | Path,
        session_id: str,
    ) -> None:
        self.session_id = session_id
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.log_dir / f"{session_id}.jsonl"
        self._lock = threading.Lock()
        self._closed = False
        # Line-buffered text mode so external tools can `tail -f` while we run
        self._fh = self.path.open("a", encoding="utf-8", buffering=1)
        logger.info(f"SessionRecorder writing to {self.path}")
        self.event("session_start", session_id=session_id)

    def event(self, kind: str, **fields: Any) -> None:
        """
        Record one event. Never raises — failures are logged and dropped so
        recording never breaks the calling code path.
        """
        if self._closed:
            return
        record = {
            "t": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "kind": kind,
        }
        record.update(fields)
        try:
            line = json.dumps(record, default=_json_default, ensure_ascii=False)
        except Exception as e:
            logger.error(f"recorder: failed to serialize event {kind}: {e}")
            return
        try:
            with self._lock:
                self._fh.write(line + "\n")
        except Exception as e:
            logger.error(f"recorder: failed to write event {kind}: {e}")

    def close(self) -> None:
        if self._closed:
            return
        self.event("session_end")
        try:
            with self._lock:
                self._fh.flush()
                self._fh.close()
        except Exception:
            pass
        finally:
            self._closed = True
            logger.info(f"SessionRecorder closed: {self.path}")

    # Context manager sugar — `with SessionRecorder(...) as rec: ...`
    def __enter__(self) -> "SessionRecorder":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


class _NullRecorder:
    """No-op recorder for callers that want optional recording."""
    def event(self, kind: str, **fields: Any) -> None: ...
    def close(self) -> None: ...


# Singleton so `recorder or NULL_RECORDER` is cheap and lets callers
# do `rec.event(...)` unconditionally without `if rec is not None`.
NULL_RECORDER = _NullRecorder()


def _json_default(o: Any) -> Any:
    """Serialize objects that aren't JSON-native."""
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, Enum):
        return o.value
    # Last-resort string fallback so we never raise from a recorder call
    return str(o)
