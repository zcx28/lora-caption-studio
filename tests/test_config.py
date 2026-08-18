from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from lora_caption_studio.config import SettingsStore


class SettingsStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_secret_is_never_returned_or_written_to_regular_settings(self) -> None:
        store = SettingsStore(self.root)
        key = "test-key-value-not-real"
        store.update(
            {
                "provider": "openai",
                "openai_model": "gpt-5.6-luna",
                "trigger": "sample_style",
                "api_key": key,
                "save_api_key": True,
            }
        )
        self.assertNotIn(key, json.dumps(store.public()))
        self.assertNotIn(key, store.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(store.api_key(), key)
        if os.name != "nt":
            self.assertEqual(store.secret_path.stat().st_mode & 0o777, 0o600)

    def test_session_key_is_not_persisted(self) -> None:
        store = SettingsStore(self.root)
        store.update(
            {
                "provider": "openai",
                "openai_model": "gpt-5.6-luna",
                "trigger": "sample_style",
                "api_key": "session-only-test-key",
                "save_api_key": False,
            }
        )
        self.assertEqual(store.api_key_source(), "session")
        self.assertFalse(store.secret_path.exists())

    def test_forget_removes_saved_key(self) -> None:
        store = SettingsStore(self.root)
        store.update(
            {
                "provider": "openai",
                "openai_model": "gpt-5.6-luna",
                "trigger": "sample_style",
                "api_key": "saved-test-key",
                "save_api_key": True,
            }
        )
        store.update({"forget_api_key": True})
        self.assertFalse(store.secret_path.exists())
        self.assertEqual(store.api_key_source(), "none")


if __name__ == "__main__":
    unittest.main()
