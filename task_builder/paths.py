from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
PROJECT_VENV = PROJECT_ROOT / ".venv"
PROJECT_PYTHON = PROJECT_VENV / "bin" / "python"
PROJECT_HARBOR = PROJECT_VENV / "bin" / "harbor"
PROJECT_CONFIG = PROJECT_ROOT / "config"
PROJECT_RUBRIC = PROJECT_CONFIG / "rubric.md"
PROJECT_PRICING = PROJECT_CONFIG / "pricing.toml"
PROJECT_OUTPUT = PROJECT_ROOT / "output"
SCIENV_IMAGE_ROOT = PROJECT_ROOT / "image"
PROJECT_RELAY = PROJECT_ROOT / "relay" / "relay.py"
ENV_IMAGE_CONFIG_NAME = "image-env.toml"
