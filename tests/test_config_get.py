"""T-45 regression tests for scripts/config_get.py and the daemon config gate.

Codex round 9 [MED] finding: watchdog.sh and start_bot.sh hard-enabled
`mlb_pitcher_scanner` regardless of config. Fix added a config_get helper
and gated the daemon on `mlb_pitcher_scanner.enabled`. These tests pin:

  1. config_get.py prints the right value for present/missing/nested paths.
  2. settings.example.yaml defines every daemon gate that start_bot.sh +
     watchdog.sh reference via config_get.py — so a future rename or
     missing section fails at test time instead of silently disabling a
     supposedly-default daemon on fresh installs.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_GET = REPO_ROOT / "scripts" / "config_get.py"
SETTINGS_EXAMPLE = REPO_ROOT / "config" / "settings.example.yaml"


def _run_config_get(config_text: str, path: str) -> str:
    """Write config_text to a temp settings.yaml and run config_get.py against it.

    We shadow the real settings.yaml via a temp file so the test does not
    depend on whatever is currently checked in.
    """
    with tempfile.TemporaryDirectory() as tmp:
        # config_get.py's CANDIDATES list expects the file at REPO/config/...
        # so we run it from a dir where config/settings.yaml points to our temp.
        cfg_dir = Path(tmp) / "config"
        cfg_dir.mkdir()
        (cfg_dir / "settings.yaml").write_text(config_text)

        # Reuse the real config_get.py but override REPO_ROOT via a one-shot
        # Python wrapper: import the module with a patched REPO_ROOT.
        wrapper = f"""
import sys
from pathlib import Path
sys.path.insert(0, {str(REPO_ROOT / "scripts")!r})
import config_get
config_get.CANDIDATES = (Path({str(cfg_dir / "settings.yaml")!r}),)
sys.argv = ["config_get.py", {path!r}]
sys.exit(config_get.main())
"""
        result = subprocess.run(
            ["python3", "-c", wrapper],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"config_get failed: {result.stderr}"
        return result.stdout.rstrip("\n")


def test_config_get_reads_nested_bool_true() -> None:
    out = _run_config_get(
        "mlb_pitcher_scanner:\n  enabled: true\n",
        "mlb_pitcher_scanner.enabled",
    )
    assert out == "true"


def test_config_get_reads_nested_bool_false() -> None:
    out = _run_config_get(
        "mlb_pitcher_scanner:\n  enabled: false\n",
        "mlb_pitcher_scanner.enabled",
    )
    assert out == "false"


def test_config_get_returns_empty_for_missing_section() -> None:
    """Missing section → empty string. Shell gate must treat this as 'not true'."""
    out = _run_config_get(
        "other_scanner:\n  enabled: true\n",
        "mlb_pitcher_scanner.enabled",
    )
    assert out == ""


def test_config_get_returns_empty_for_missing_key_in_present_section() -> None:
    out = _run_config_get(
        "mlb_pitcher_scanner:\n  hours_window: 48\n",
        "mlb_pitcher_scanner.enabled",
    )
    assert out == ""


def test_config_get_reads_scalar_string() -> None:
    """Non-boolean values render via str() — must work for string flags."""
    out = _run_config_get(
        "foo:\n  bar: hello\n",
        "foo.bar",
    )
    assert out == "hello"


# ─────────────────────────────────────────────────────────────────────────────
# Static contract: every daemon gate in shell scripts exists in settings.example
# ─────────────────────────────────────────────────────────────────────────────


def _find_config_get_paths() -> set[str]:
    """Parse start_bot.sh + watchdog.sh for `config_get.py <path>` invocations
    and return the set of dotted paths they reference."""
    paths: set[str] = set()
    for script in ("scripts/start_bot.sh", "scripts/watchdog.sh"):
        src = (REPO_ROOT / script).read_text()
        paths.update(re.findall(
            r'config_get\.py"?\s+([a-zA-Z_][\w.]*)',
            src,
        ))
    return paths


def test_every_shell_config_gate_is_defined_in_settings_example() -> None:
    """Round-9 [MED] regression: if a shell script reads
    `config_get.py mlb_pitcher_scanner.enabled`, settings.example.yaml must
    define that section — otherwise a fresh install silently disables the
    daemon (or, flipped, leaves it in the wrong state by default).

    This fires at commit time if any future gate is added to a shell
    script without a matching example-config entry."""
    paths = _find_config_get_paths()
    assert paths, "No config_get.py invocations found — test logic broken or feature removed"

    import yaml
    cfg = yaml.safe_load(SETTINGS_EXAMPLE.read_text())

    missing = []
    for dotted in paths:
        node = cfg
        ok = True
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                ok = False
                break
            node = node[part]
        if not ok:
            missing.append(dotted)

    assert not missing, (
        "Shell scripts reference config paths not defined in settings.example.yaml:\n"
        + "\n".join(f"  - {p}" for p in missing)
        + "\nAdd these sections so fresh installs get the intended default."
    )


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
