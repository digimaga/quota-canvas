"""Existing engine paths; platform-specific replacements can be added here."""

from pathlib import Path


def config_path() -> Path:
    return Path.home() / ".config" / "quota-canvas" / "config.toml"


def data_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "quota-canvas"
