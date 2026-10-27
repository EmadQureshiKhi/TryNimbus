"""Settings and secrets.

Secrets come from the environment (a `.env` at the repo root or `~/.nimbus/.env` is loaded
first), then the macOS Keychain. Everything else lives in `~/.nimbus/settings.json`, which the
side panel edits. Unknown keys in that file are ignored and missing ones fall back to the
defaults below, so an old settings file never breaks a newer build.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("NIMBUS_HOME", Path.home() / ".nimbus"))
SETTINGS_PATH = HOME / "settings.json"
KEYCHAIN_SERVICE = "nimbus"

load_dotenv(REPO_ROOT / ".env", override=False)
load_dotenv(HOME / ".env", override=False)


@dataclass
class Models:
    """Meta model ids. Each role is an ordered fallback chain: the first id is tried first."""

    ask: list[str] = field(default_factory=lambda: ["muse-spark-1.3", "muse-spark-1.2"])
    grounding: list[str] = field(default_factory=lambda: ["muse-spark-1.3", "muse-spark-1.2"])
    # Spark 1.3 audio is "not fully supported" per the docs, so narrated video goes to 1.2 first.
    video: list[str] = field(default_factory=lambda: ["muse-spark-1.2", "muse-spark-1.3"])
    verify: list[str] = field(default_factory=lambda: ["muse-spark-1.3", "muse-spark-1.2"])
    computer_use: list[str] = field(default_factory=lambda: ["muse-spark-1.3", "muse-wonder-1.0"])
    coder: list[str] = field(default_factory=lambda: ["muse-spark-1.3", "muse-wonder-1.0"])
    sam: str = "sam-3.1"
    transcribe: str = "muse-voice-transcribe-1.0"


@dataclass
class OpenWeights:
    """Muse Spark Open served by an inference partner (OpenAI-compatible)."""

    enabled: bool = False
    base_url: str = "https://inference.baseten.co/v1"
    model: str = "meta-models/muse-spark-open"
    api_key_env: str = "BASETEN_API_KEY"
    # Which roles run on open weights when enabled.
    roles: list[str] = field(default_factory=lambda: ["verify", "bug_analysis"])


@dataclass
class ReproSettings:
    repo_path: str = str(REPO_ROOT / "runs" / "demo" / "shopapp")  # made by tools/demo_repo.py
    app_url: str = "http://127.0.0.1:5173"
    start_command: str = "python3 -m http.server 5173 --bind 127.0.0.1"
    test_command: str = "node --test"
    use_muse_code: bool = True
    open_pull_request: bool = False


@dataclass
class Settings:
    hotkey: str = "<ctrl>+<alt>+<space>"
    speak_answers: bool = True
    voice_name: str = ""
    buddy_enabled: bool = True
    refine_pointing: bool = True
    ask_effort: str = "minimal"  # same accuracy as "low" in the pointing evals, about 2 s faster
    solve_effort: str = "medium"  # questions that ask Nimbus to work a problem out (see ask.needs_working)
    confirm_before_acting: bool = True
    max_agent_steps: int = 25
    panel_y_fraction: float = 0.38
    collapsed_sections: list[str] = field(default_factory=list)
    models: Models = field(default_factory=Models)
    open_weights: OpenWeights = field(default_factory=OpenWeights)
    repro: ReproSettings = field(default_factory=ReproSettings)


def _merge(cls: type, data: dict[str, Any]) -> Any:
    """Build dataclass `cls` from `data`, recursing into nested dataclasses, ignoring junk."""
    kwargs = {}
    defaults = cls()
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        current = getattr(defaults, f.name)
        if is_dataclass(current) and isinstance(value, dict):
            kwargs[f.name] = _merge(type(current), value)
        elif isinstance(current, bool):
            kwargs[f.name] = bool(value)
        elif isinstance(current, (int, float)) and isinstance(value, (int, float)):
            kwargs[f.name] = type(current)(value)
        elif isinstance(current, (str, list)) and isinstance(value, type(current)):
            kwargs[f.name] = value
    return cls(**kwargs)


class SettingsStore:
    """Thread-safe load/save of `Settings` to JSON."""

    def __init__(self, path: Path = SETTINGS_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.settings = self._load()

    def _load(self) -> Settings:
        try:
            data = json.loads(self.path.read_text())
            return _merge(Settings, data)
        except FileNotFoundError:
            return Settings()
        except (OSError, ValueError, TypeError) as exc:
            log.warning("settings file unreadable (%s); using defaults", exc)
            return Settings()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self.settings), indent=2))
            tmp.replace(self.path)


def get_secret(name: str) -> str | None:
    """Environment first, then the macOS Keychain."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    try:
        import keyring

        stored = keyring.get_password(KEYCHAIN_SERVICE, name)
        return stored.strip() if stored else None
    except Exception as exc:  # keyring backends raise a zoo of types
        log.debug("keychain lookup failed for %s: %s", name, exc)
        return None


def set_secret(name: str, value: str) -> None:
    import keyring

    keyring.set_password(KEYCHAIN_SERVICE, name, value.strip())
    os.environ[name] = value.strip()


def data_dir(*parts: str) -> Path:
    path = HOME.joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path
