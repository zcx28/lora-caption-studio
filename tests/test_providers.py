from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from lora_caption_studio import application
from lora_caption_studio.providers import CodexCLIProvider, OpenAIAPIProvider, find_codex_cli


class ProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.images = []
        for index in range(2):
            path = self.root / f"image-{index}.jpg"
            Image.new("RGB", (24, 24), "white").save(path)
            self.images.append(path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_codex_command_uses_repeatable_image_flags_and_structured_output(self) -> None:
        captured: list[str] = []

        def runner(command, timeout, cwd):
            del timeout, cwd
            captured.extend(command)
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps({"captions": []}), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        provider = CodexCLIProvider(binary="codex-test", command_runner=runner)
        payload = provider.generate(
            prompt="test",
            image_paths=self.images,
            schema=application.result_schema(),
            timeout=30,
        )
        self.assertEqual(payload, {"captions": []})
        self.assertEqual(captured.count("--image"), 2)
        self.assertIn("--output-schema", captured)
        self.assertEqual(captured[0], "codex-test")

    def test_desktop_app_internal_codex_is_rejected(self) -> None:
        bundled = "/Example/Product.app/Contents/Resources/codex"
        with patch("lora_caption_studio.providers.shutil.which", return_value=bundled), patch(
            "lora_caption_studio.providers.Path.resolve", return_value=Path(bundled)
        ):
            self.assertIsNone(find_codex_cli())

    def test_codex_subprocess_can_be_cancelled_cross_platform(self) -> None:
        provider = CodexCLIProvider(binary=sys.executable)
        outcome: dict[str, object] = {}

        def run() -> None:
            try:
                provider._run([sys.executable, "-c", "import time; time.sleep(30)"], 40, self.root)
            except BaseException as exc:
                outcome["error"] = exc

        worker = threading.Thread(target=run)
        worker.start()
        deadline = time.monotonic() + 3
        while not provider._active and time.monotonic() < deadline:
            time.sleep(0.01)
        provider.cancel()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(outcome.get("error"), InterruptedError)

    def test_openai_provider_sends_images_and_parses_structured_output(self) -> None:
        calls: list[dict[str, object]] = []
        response_payload = {"captions": []}

        class FakeResponses:
            def create(self, **kwargs):
                calls.append(kwargs)
                return types.SimpleNamespace(output_text=json.dumps(response_payload))

        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.responses = FakeResponses()

        fake_module = types.SimpleNamespace(OpenAI=FakeOpenAI)
        with patch.dict(sys.modules, {"openai": fake_module}):
            provider = OpenAIAPIProvider(api_key="test-key", model="gpt-5.6-luna")
            payload = provider.generate(
                prompt="test",
                image_paths=self.images,
                schema=application.result_schema(),
                timeout=30,
            )
        self.assertEqual(payload, response_payload)
        content = calls[0]["input"][0]["content"]
        self.assertEqual(sum(item["type"] == "input_image" for item in content), 2)
        self.assertTrue(all(item.get("image_url", "").startswith("data:image/") for item in content[1:]))


if __name__ == "__main__":
    unittest.main()
