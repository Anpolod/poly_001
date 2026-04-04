"""
config/validate.py

Startup config validation. Call validate_config(config) before any I/O.
Raises SystemExit with a clear message on the first missing or wrong-type key.
"""

import sys
from typing import Any


# (dotted_key, expected_type, description)
_REQUIRED: list[tuple[str, type, str]] = [
    ("database.host",                          str,   "DB hostname"),
    ("database.port",                          int,   "DB port"),
    ("database.name",                          str,   "DB name"),
    ("database.user",                          str,   "DB user"),
    ("database.password",                      str,   "DB password (or set DB_PASSWORD env var)"),
    ("api.gamma_base_url",                     str,   "Gamma API base URL"),
    ("api.clob_base_url",                      str,   "CLOB API base URL"),
    ("api.ws_url",                             str,   "WebSocket URL"),
    ("phase0.min_volume_24h",                  (int, float), "Phase 0 min 24h volume ($)"),
    ("phase0.ratio_go_threshold",              (int, float), "Phase 0 GO ratio threshold"),
    ("phase0.ratio_marginal_threshold",        (int, float), "Phase 0 MARGINAL ratio threshold"),
    ("phase1.snapshot_interval_sec",           int,   "Phase 1 snapshot interval (seconds)"),
    ("phase1.market_rescan_interval_sec",      int,   "Phase 1 market rescan interval (seconds)"),
    ("collector.ws_reconnect_base_delay",      (int, float), "WS reconnect base delay (seconds)"),
    ("collector.ws_reconnect_max_delay",       (int, float), "WS reconnect max delay (seconds)"),
]


def _get_nested(config: dict, dotted_key: str) -> Any:
    """Traverse nested dict by dotted key. Returns _MISSING sentinel if absent."""
    parts = dotted_key.split(".")
    node = config
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


_MISSING = object()


def validate_config(config: dict) -> None:
    """
    Check all required config keys exist and have the expected types.
    Prints all errors at once, then exits with code 1 if any found.
    """
    errors: list[str] = []

    for dotted_key, expected_type, description in _REQUIRED:
        value = _get_nested(config, dotted_key)

        if value is _MISSING:
            errors.append(
                f"  MISSING  {dotted_key!r:45s}  — {description}"
            )
            continue

        if not isinstance(value, expected_type):
            type_name = (
                " or ".join(t.__name__ for t in expected_type)
                if isinstance(expected_type, tuple)
                else expected_type.__name__
            )
            errors.append(
                f"  WRONG TYPE  {dotted_key!r:42s}  — expected {type_name}, "
                f"got {type(value).__name__!r} ({value!r})"
            )

    if errors:
        print("CONFIG ERROR — fix config/settings.yaml before starting:\n")
        for e in errors:
            print(e)
        sys.exit(1)
