from __future__ import annotations

import re
import tempfile
import threading
import time
import unittest
from pathlib import Path

from PIL import Image

from lora_caption_studio import application
from lora_caption_studio.config import SettingsStore
from lora_caption_studio.providers import AIProvider


def make_caption(trigger: str, subject: str, words: int = 72) -> str:
    base = (
        f"{trigger}. A {subject} stands near a simple object with a relaxed posture, facing slightly toward "
        "the viewer while keeping both hands visible. The figure wears plain clothing, including a buttoned "
        "shirt, straight trousers, and low shoes. The left hand rests on the object while the right arm bends "
        "at the elbow. Short hair, a small nose, and a closed mouth are clearly visible."
    )
    tokens = base.split()
    while application.word_count(" ".join(tokens), trigger) < words:
        tokens.extend("The visible arrangement remains uncluttered and easy to identify.".split())
    return " ".join(tokens)


class FakeProvider(AIProvider):
    provider_id = "fake"
    display_name = "Fake Provider"

    def __init__(self, *, delay: float = 0) -> None:
        self.delay = delay
        self.cancelled = False
        self.prompts: list[str] = []
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()

    @property
    def model_label(self) -> str:
        return "fake-model"

    def readiness(self) -> tuple[bool, str]:
        return True, "ready"

    def generate(self, *, prompt, image_paths, schema, timeout):
        del schema, timeout
        self.prompts.append(prompt)
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.cancelled:
                raise InterruptedError("paused")
            ids = re.findall(r"sha_id=([^\s]+)", prompt)
            trigger_match = re.search(r"starts exactly with `([^`]+)\.\`", prompt)
            trigger = trigger_match.group(1) if trigger_match else "my_style"
            return {
                "captions": [
                    {
                        "sha_id": item_id,
                        "caption": make_caption(trigger, path.stem),
                        "zh_translation": f"{path.stem} 的中文参考译文。",
                    }
                    for item_id, path in zip(ids, image_paths)
                ]
            }
        finally:
            with self.lock:
                self.active -= 1

    def cancel(self) -> None:
        self.cancelled = True


class CaptionerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.images = self.root / "images"
        self.state = self.root / "state"
        self.images.mkdir()
        for index, name in enumerate(("image 2.jpg", "image 10.jpg", "portrait.png", "sample.webp"), start=1):
            Image.new("RGB", (72, 64), (245, 245 - index, 240)).save(self.images / name)
        self.provider = FakeProvider()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def app(self, *, provider: AIProvider | None = None, batch_size: int = 8, concurrency: int = 4):
        application.ensure_manifest(self.images, self.state)
        return application.CaptionerApplication(
            self.images,
            self.state,
            provider=provider or self.provider,
            trigger="sample_style",
            timeout=30,
            batch_size=batch_size,
            concurrency=concurrency,
        )

    def test_plain_folder_manifest_is_stable_and_images_are_not_renamed(self) -> None:
        app = self.app()
        self.assertEqual(
            [item["current_file"] for item in app.public_data()["items"]],
            ["image 2.jpg", "image 10.jpg", "portrait.png", "sample.webp"],
        )
        self.assertEqual(app.prepare_dataset()["count"], 4)
        self.assertTrue((self.images / "image 2.jpg").exists())
        self.assertFalse(any(path.name.startswith("sample_style_") for path in self.images.iterdir()))

    def test_dynamic_trigger_validation_and_warnings(self) -> None:
        caption = make_caption("sample_style", "person")
        self.assertEqual(application.validate_caption(caption, "sample_style"), caption)
        self.assertEqual(application.caption_warnings(caption, "sample_style"), [])
        with self.assertRaisesRegex(ValueError, "sample_style"):
            application.validate_caption(make_caption("wrong_style", "person"), "sample_style")

    def test_generation_writes_english_caption_and_keeps_translation_in_state(self) -> None:
        app = self.app()
        app._process_batch(app.order[:2])
        for item_id in app.order[:2]:
            item = app.items[item_id]
            self.assertEqual(item["status"], "pending")
            self.assertTrue(str(item["translation"]).endswith("中文参考译文。"))
            caption_text = app._caption_path(item).read_text(encoding="utf-8")
            self.assertTrue(caption_text.startswith("sample_style."))
            self.assertNotIn("中文参考译文", caption_text)

    def test_selected_items_and_chinese_guidance_reach_provider(self) -> None:
        app = self.app()
        app._start_worker = lambda: None  # type: ignore[method-assign]
        chosen = [app.order[1], app.order[3]]
        result = app.start_selected_items(chosen, "强调人物手里的红色雨伞")
        self.assertEqual(result["queued"], 2)
        self.assertEqual(app.priority_ids, chosen)
        self.assertEqual(app.guidance, "强调人物手里的红色雨伞")
        self.assertIn("sample_style", application.build_prompt([], app.guidance, app.trigger))

    def test_existing_human_caption_is_not_overwritten_without_force(self) -> None:
        app = self.app()
        item = app.items[app.order[0]]
        human_caption = make_caption("sample_style", "human edited subject")
        app._caption_path(item).write_text(human_caption + "\n", encoding="utf-8")
        item["managed_caption"] = False
        with self.assertRaisesRegex(ValueError, "不会覆盖"):
            app._write_generated_caption(item, make_caption("sample_style", "different subject"))

    def test_pause_cancels_provider_and_requeues_current_batch(self) -> None:
        provider = FakeProvider(delay=0.2)
        app = self.app(provider=provider)
        batch_ids = [app.order[0]]
        worker = threading.Thread(target=app._process_batch, args=(batch_ids,))
        worker.start()
        deadline = time.monotonic() + 2
        while provider.active == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        app.pause()
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(provider.cancelled)
        self.assertEqual(app.items[batch_ids[0]]["status"], "ungenerated")
        self.assertIn(batch_ids[0], app.priority_ids)

    def test_batches_can_run_concurrently(self) -> None:
        provider = FakeProvider(delay=0.15)
        app = self.app(provider=provider, batch_size=1, concurrency=2)
        app.start_selected_items(app.order)
        assert app.worker
        app.worker.join(timeout=5)
        self.assertFalse(app.worker and app.worker.is_alive())
        self.assertGreaterEqual(provider.peak, 2)
        self.assertTrue(all(app.items[item_id]["status"] == "pending" for item_id in app.order))

    def test_manager_switches_plain_folders_with_local_settings(self) -> None:
        settings = SettingsStore(self.root / "config")
        settings.update({"provider": "codex", "openai_model": "gpt-5.6-luna", "trigger": "sample_style"})
        manager = application.CaptionerManager(
            self.images,
            state_root=self.root / "app-state",
            settings_store=settings,
            provider=self.provider,
        )
        self.assertEqual(len(manager.public_data()["items"]), 4)
        other = self.root / "other"
        other.mkdir()
        Image.new("RGB", (32, 32), "white").save(other / "new.jpg")
        manager.select_directory(other)
        self.assertEqual(manager.current().selected_dir, other.resolve())
        self.assertEqual(len(manager.public_data()["items"]), 1)


if __name__ == "__main__":
    unittest.main()
