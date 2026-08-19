"""Local-only configuration and data paths for LoRA Caption Studio."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any


APP_DIR_NAME = "LoRA Caption Studio"
DEFAULT_OPENAI_MODEL = "gpt-5.6-luna"
DEFAULT_TRIGGER = "my_style"
PROVIDER_IDS = {"codex", "openai"}


def user_config_dir() -> Path:
    """Return a per-user config directory without assuming a specific username."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "lora-caption-studio"


def user_state_dir() -> Path:
    """Return a per-user data directory for manifests, state, and thumbnails."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / APP_DIR_NAME / "Data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME / "Data"
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "lora-caption-studio"


def _atomic_write_json(path: Path, payload: object, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if private:
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
        os.replace(temporary, path)
        if private:
            try:
                path.chmod(0o600)
            except OSError:
                pass
    finally:
        temporary.unlink(missing_ok=True)


class SettingsStore:
    """Keep non-secret preferences and optional credentials outside the repository."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or user_config_dir()).expanduser().resolve()
        self.settings_path = self.root / "settings.json"
        self.secret_path = self.root / "openai_credentials.json"
        self._session_api_key = ""
        self._settings = self._read_json(self.settings_path)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    @property
    def provider_id(self) -> str:
        value = str(self._settings.get("provider", os.environ.get("LCS_PROVIDER", "codex"))).strip().lower()
        return value if value in PROVIDER_IDS else "codex"

    @property
    def openai_model(self) -> str:
        return str(
            self._settings.get("openai_model", os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL))
        ).strip() or DEFAULT_OPENAI_MODEL

    @property
    def codex_model(self) -> str:
        return str(self._settings.get("codex_model", os.environ.get("LCS_CODEX_MODEL", ""))).strip()

    @property
    def trigger(self) -> str:
        return str(self._settings.get("trigger", os.environ.get("LCS_TRIGGER", DEFAULT_TRIGGER))).strip() or DEFAULT_TRIGGER

    def api_key(self) -> str:
        if self._session_api_key:
            return self._session_api_key
        environment_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if environment_key:
            return environment_key
        return str(self._read_json(self.secret_path).get("api_key", "")).strip()

    def api_key_source(self) -> str:
        if self._session_api_key:
            return "session"
        if os.environ.get("OPENAI_API_KEY", "").strip():
            return "environment"
        if self._read_json(self.secret_path).get("api_key"):
            return "local_file"
        return "none"

    def update(self, payload: dict[str, Any]) -> None:
        provider = str(payload.get("provider", self.provider_id)).strip().lower()
        if provider not in PROVIDER_IDS:
            raise ValueError("不支持的 AI Provider")
        model = str(payload.get("openai_model", self.openai_model)).strip()
        if not model:
            raise ValueError("OpenAI 模型不能为空")
        trigger = str(payload.get("trigger", self.trigger)).strip()
        if not trigger or len(trigger) > 120 or any(character in trigger for character in "\r\n"):
            raise ValueError("触发词必须是 1–120 个字符的单行文本")

        self._settings = {
            "provider": provider,
            "openai_model": model,
            "codex_model": str(payload.get("codex_model", self.codex_model)).strip(),
            "trigger": trigger,
        }
        _atomic_write_json(self.settings_path, self._settings)

        if bool(payload.get("forget_api_key")):
            self._session_api_key = ""
            self.secret_path.unlink(missing_ok=True)

        submitted_key = str(payload.get("api_key", "")).strip()
        if submitted_key:
            self._session_api_key = submitted_key
            if bool(payload.get("save_api_key")):
                _atomic_write_json(self.secret_path, {"api_key": submitted_key}, private=True)
            else:
                self.secret_path.unlink(missing_ok=True)

    def public(self) -> dict[str, object]:
        source = self.api_key_source()
        return {
            "provider": self.provider_id,
            "openai_model": self.openai_model,
            "codex_model": self.codex_model,
            "trigger": self.trigger,
            "api_key_configured": source != "none",
            "api_key_source": source,
        }
