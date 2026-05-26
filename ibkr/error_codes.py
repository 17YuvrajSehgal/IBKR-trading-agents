"""
Authoritative IBKR API message classification.

Each message TWS sends to the client has a numeric code. Many of them are
not actually errors — they're heartbeats from data farms or informational
warnings. This module classifies every code we care about by

    Severity  : INFO | WARNING | ERROR | FATAL
    Category  : SYSTEM | CONNECTION | DATA_FARM | ORDER | DATA | PACING | UNKNOWN

so callers can act appropriately:

    info = classify(2108)
    if info.severity is Severity.INFO:
        logger.debug(info.description)       # data-farm heartbeat — quiet it
    elif info.category is Category.CONNECTION and info.auto_recovers:
        attempt_reconnect()
    elif info.severity is Severity.FATAL:
        shutdown()

Reference: https://interactivebrokers.github.io/tws-api/message_codes.html

This module is pure data — no broker dependency, no I/O — so it can be unit
tested standalone.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    FATAL = "FATAL"


class Category(str, Enum):
    SYSTEM = "SYSTEM"          # server-level status messages
    CONNECTION = "CONNECTION"  # connection up/down lifecycle (1100s)
    DATA_FARM = "DATA_FARM"    # data farm status pings (2100s)
    ORDER = "ORDER"            # order placement / lifecycle
    DATA = "DATA"              # market / historical / news data
    PACING = "PACING"          # request-rate violations
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class MessageInfo:
    code: int
    severity: Severity
    category: Category
    description: str
    auto_recovers: bool = False  # True if system resolves without action


# --- Authoritative table -----------------------------------------------------
# Codes are split into ranges by category for readability.

_TABLE: dict[int, MessageInfo] = {

    # ---------- Connection lifecycle (1100-series) -------------------------
    1100: MessageInfo(1100, Severity.WARNING, Category.CONNECTION,
                      "Connectivity between IB and TWS lost",
                      auto_recovers=True),
    1101: MessageInfo(1101, Severity.WARNING, Category.CONNECTION,
                      "Connectivity restored — data lost (resubscribe market data)",
                      auto_recovers=True),
    1102: MessageInfo(1102, Severity.INFO, Category.CONNECTION,
                      "Connectivity restored — data maintained",
                      auto_recovers=True),
    1300: MessageInfo(1300, Severity.ERROR, Category.CONNECTION,
                      "TWS socket port reset; connection dropped"),

    # ---------- System / data farm status (2000-series) --------------------
    # These fire during the nightly server reset and as data farms cycle.
    # Most are purely informational.
    2100: MessageInfo(2100, Severity.WARNING, Category.SYSTEM,
                      "API client cannot be moved to a different account"),
    2103: MessageInfo(2103, Severity.WARNING, Category.DATA_FARM,
                      "Market data farm connection is broken",
                      auto_recovers=True),
    2104: MessageInfo(2104, Severity.INFO, Category.DATA_FARM,
                      "Market data farm connection is OK",
                      auto_recovers=True),
    2105: MessageInfo(2105, Severity.WARNING, Category.DATA_FARM,
                      "HMDS data farm connection is broken",
                      auto_recovers=True),
    2106: MessageInfo(2106, Severity.INFO, Category.DATA_FARM,
                      "HMDS data farm connection is OK",
                      auto_recovers=True),
    2107: MessageInfo(2107, Severity.INFO, Category.DATA_FARM,
                      "HMDS data farm connection inactive but available",
                      auto_recovers=True),
    2108: MessageInfo(2108, Severity.INFO, Category.DATA_FARM,
                      "Market data farm connection inactive but available",
                      auto_recovers=True),
    2109: MessageInfo(2109, Severity.WARNING, Category.ORDER,
                      "Order Event Warning — outside RTH"),
    2110: MessageInfo(2110, Severity.WARNING, Category.CONNECTION,
                      "Connectivity between TWS and server is broken",
                      auto_recovers=True),
    2119: MessageInfo(2119, Severity.INFO, Category.DATA_FARM,
                      "Market data farm is connecting"),
    2150: MessageInfo(2150, Severity.WARNING, Category.DATA,
                      "Invalid position trade derived value"),
    2152: MessageInfo(2152, Severity.WARNING, Category.DATA,
                      "Cross side warning"),
    2157: MessageInfo(2157, Severity.WARNING, Category.DATA_FARM,
                      "Sec-def data farm connection is broken",
                      auto_recovers=True),
    2158: MessageInfo(2158, Severity.INFO, Category.DATA_FARM,
                      "Sec-def data farm connection is OK",
                      auto_recovers=True),
    2168: MessageInfo(2168, Severity.INFO, Category.DATA_FARM,
                      "Sec-def data farm inactive but available",
                      auto_recovers=True),
    2169: MessageInfo(2169, Severity.INFO, Category.DATA_FARM,
                      "Sec-def data farm inactive but available",
                      auto_recovers=True),
    2174: MessageInfo(2174, Severity.WARNING, Category.ORDER,
                      "Order placed with date-time + time-zone attributes"),

    # ---------- Order / contract --------------------------------------------
    200: MessageInfo(200, Severity.ERROR, Category.ORDER,
                     "No security definition found for the request"),
    201: MessageInfo(201, Severity.ERROR, Category.ORDER,
                     "Order rejected — see message"),
    202: MessageInfo(202, Severity.INFO, Category.ORDER,
                     "Order cancelled"),
    203: MessageInfo(203, Severity.ERROR, Category.ORDER,
                     "Security not available or allowed for this account"),
    321: MessageInfo(321, Severity.ERROR, Category.ORDER,
                     "Server error validating request"),
    322: MessageInfo(322, Severity.ERROR, Category.ORDER,
                     "Error processing request"),
    354: MessageInfo(354, Severity.ERROR, Category.DATA,
                     "Requested market data is not subscribed"),
    399: MessageInfo(399, Severity.WARNING, Category.ORDER,
                     "Order message"),
    404: MessageInfo(404, Severity.WARNING, Category.ORDER,
                     "Order held while securities are located"),

    # 10000-series modern order/data messages
    10147: MessageInfo(10147, Severity.WARNING, Category.ORDER,
                       "Order ID to cancel was not found"),
    10148: MessageInfo(10148, Severity.WARNING, Category.ORDER,
                       "Order ID to cancel cannot be cancelled"),
    10167: MessageInfo(10167, Severity.WARNING, Category.DATA,
                       "Displaying delayed market data"),
    10197: MessageInfo(10197, Severity.WARNING, Category.DATA,
                       "No market data permissions for this contract"),
    10349: MessageInfo(10349, Severity.INFO, Category.ORDER,
                       "Order TIF set to DAY by preset"),

    # ---------- Pacing / quotas --------------------------------------------
    162: MessageInfo(162, Severity.ERROR, Category.PACING,
                     "Historical market data — pacing violation"),
    165: MessageInfo(165, Severity.WARNING, Category.PACING,
                     "Historical data service messaging"),
    420: MessageInfo(420, Severity.ERROR, Category.PACING,
                     "Too many simultaneous API historical requests"),

    # ---------- Hard connection errors --------------------------------------
    326: MessageInfo(326, Severity.FATAL, Category.CONNECTION,
                     "Client ID already in use — choose another"),
    501: MessageInfo(501, Severity.FATAL, Category.CONNECTION,
                     "Already connected"),
    502: MessageInfo(502, Severity.FATAL, Category.CONNECTION,
                     "Couldn't connect to TWS — check API enabled and port"),
    504: MessageInfo(504, Severity.FATAL, Category.CONNECTION,
                     "Not connected"),
    509: MessageInfo(509, Severity.ERROR, Category.CONNECTION,
                     "Exception caught while reading socket"),
}


def classify(code: int) -> MessageInfo:
    """
    Return the MessageInfo for an IBKR code.

    Unknown codes default to (WARNING, UNKNOWN) so they're visible but don't
    halt the program. Add new codes to _TABLE as you encounter them in logs.
    """
    return _TABLE.get(
        code,
        MessageInfo(
            code=code,
            severity=Severity.WARNING,
            category=Category.UNKNOWN,
            description="Unclassified message — see IBKR docs",
        ),
    )


def is_known(code: int) -> bool:
    """True if `code` is in our table."""
    return code in _TABLE
