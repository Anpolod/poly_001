"""Network helpers — aiohttp session factory with optional local IP binding.

On macOS with multiple network interfaces (e.g. inactive Ethernet + active Wi-Fi),
source address auto-selection for outbound TCP may fail with EADDRNOTAVAIL.
Setting LOCAL_BIND_IP in .env forces aiohttp to bind to the correct interface.

Usage:
    from collector.network import make_session, make_connector

    async with make_session(timeout=my_timeout) as session:
        ...

    # or for a persistent session:
    connector = make_connector()
    session = aiohttp.ClientSession(connector=connector, timeout=my_timeout)
"""

from __future__ import annotations

import logging
import os
import socket

import aiohttp

logger = logging.getLogger(__name__)

_local_ip: str | None = None
_detection_done = False


def _get_local_ip() -> str | None:
    """Return the local IP to bind to, or None if not needed.

    Priority:
      1. LOCAL_BIND_IP env var (explicit override, e.g. in Mac Mini .env)
      2. UDP-socket trick: connect to a public IP (no packets sent) to
         discover which local address the OS *would* use. Falls back to
         None if the trick itself fails (dev environment, Linux, etc.)
    """
    global _local_ip, _detection_done
    if _detection_done:
        return _local_ip

    _detection_done = True
    env_ip = os.environ.get("LOCAL_BIND_IP", "").strip()
    if env_ip:
        _local_ip = env_ip
        logger.debug("LOCAL_BIND_IP set to %s (from env)", _local_ip)
        return _local_ip

    # UDP connect does not send packets — it just asks the kernel for a route.
    # If this succeeds we get the source IP the OS would use for TCP too.
    # If the OS has the same routing bug for UDP, getsockname returns 0.0.0.0
    # and we skip binding (caller falls back to no-bind behaviour).
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and ip != "0.0.0.0":
                _local_ip = ip
                logger.debug("Auto-detected local IP: %s", _local_ip)
    except Exception as exc:
        logger.debug("Could not auto-detect local IP: %s", exc)

    return _local_ip


def make_connector(**kwargs) -> aiohttp.TCPConnector:
    """Return a TCPConnector bound to the local outbound IP if needed."""
    local_ip = _get_local_ip()
    if local_ip:
        return aiohttp.TCPConnector(local_addr=(local_ip, 0), **kwargs)
    return aiohttp.TCPConnector(**kwargs)


def make_session(**kwargs) -> aiohttp.ClientSession:
    """Return an aiohttp.ClientSession with the correct local IP binding.

    Accepts the same keyword arguments as aiohttp.ClientSession (timeout, headers, …).
    Do NOT pass connector= — this function manages it.

    Example:
        async with make_session(timeout=aiohttp.ClientTimeout(total=10)) as s:
            resp = await s.get(url)
    """
    connector = make_connector()
    return aiohttp.ClientSession(connector=connector, **kwargs)
