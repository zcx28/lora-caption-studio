#!/usr/bin/env python3
"""Local web application for reviewing AI-generated LoRA captions."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from PIL import Image, ImageOps, UnidentifiedImageError

from .config import DEFAULT_TRIGGER, SettingsStore, user_state_dir
from .providers import AIProvider, CodexCLIProvider, create_provider


TRIGGER = DEFAULT_TRIGGER
DEFAULT_BATCH_SIZE = 8
DEFAULT_CONCURRENCY = 4
DEFAULT_TIMEOUT = 8 * 60
MAX_RETRIES = 2
STATE_VERSION = 1
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
STATUSES = {"ungenerated", "generating", "pending", "confirmed", "failed"}
DEFAULT_STATE_ROOT = user_state_dir()
DEFAULT_GUIDANCE = (
    "重点描述每张图独有的可见内容。不要添加‘人物位于画面中心’‘周围有大量/充足留白’一类"
    "几乎每张图都适用的通用构图总结，也不要为了凑字数重复背景、居中或留白。只有位置明显异常，"
    "或位置对人物动作、人物与物体关系很重要时，才描述构图位置。"
)
STYLE_TERMS = {
    "anime style",
    "art style",
    "best quality",
    "highly detailed",
    "line art",
    "masterpiece",
    "oil painting",
    "sketch",
    "watercolor",
    "3d render",
}
GENERIC_COMPOSITION_PHRASES = {
    "ample white space",
    "broad empty space",
    "generous empty space",
    "generous open space",
    "generous white space",
    "placed in the center of the frame",
    "positioned in the center of the frame",
    "the figure is centered in the frame",
    "the figure occupies the central area",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: object) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def write_csv_atomic(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"CSV 没有表头：{path}")
        return list(reader), list(reader.fieldnames)


def manifest_item_id(row: dict[str, str]) -> str:
    return row.get("id") or row["sha256"]


def natural_sort_key(value: str) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def default_state_dir(selected_dir: Path, state_root: Path = DEFAULT_STATE_ROOT) -> Path:
    resolved = selected_dir.expanduser().resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    safe_name = re.sub(r"[^\w.-]+", "-", resolved.name, flags=re.UNICODE).strip("-.") or "dataset"
    return state_root.expanduser().resolve() / "datasets" / f"{safe_name}-{digest}"


def ensure_manifest(selected_dir: Path, state_dir: Path) -> Path:
    """Create a stable manifest for an ordinary image folder when none exists."""
    selected_dir = selected_dir.expanduser().resolve()
    state_dir = state_dir.expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "selection_manifest.csv"
    if target.is_file():
        return target
    legacy = selected_dir / "selection_manifest.csv"
    if legacy.is_file():
        shutil.copy2(legacy, target)
        return target

    image_paths = sorted(
        (path for path in selected_dir.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS),
        key=lambda path: natural_sort_key(path.name),
    )
    if not image_paths:
        raise ValueError("所选文件夹中没有支持的图片（JPG、PNG 或 WebP）")
    seen_hashes: dict[str, int] = {}
    rows: list[dict[str, str]] = []
    for image_path in image_paths:
        digest = sha256_file(image_path)
        occurrence = seen_hashes.get(digest, 0) + 1
        seen_hashes[digest] = occurrence
        item_id = digest if occurrence == 1 else f"{digest}-{occurrence}"
        rows.append(
            {
                "id": item_id,
                "file": image_path.name,
                "original_file": image_path.name,
                "sha256": digest,
            }
        )
    write_csv_atomic(target, rows, ["id", "file", "original_file", "sha256"])
    return target


def normalize_caption(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def word_count(caption: str, trigger: str = TRIGGER) -> int:
    body = caption.removeprefix(f"{trigger}.").strip()
    return len(re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*", body))


def caption_warnings(caption: str, trigger: str = TRIGGER) -> list[str]:
    warnings: list[str] = []
    count = word_count(caption, trigger)
    if count < 60:
        warnings.append(f"少于 60 词（当前 {count}）")
    elif count > 200:
        warnings.append(f"超过 200 词（当前 {count}）")
    lowered = caption.lower()
    found = sorted(term for term in STYLE_TERMS if term in lowered)
    if found:
        warnings.append("正文可能含风格词：" + "、".join(found))
    generic = sorted(phrase for phrase in GENERIC_COMPOSITION_PHRASES if phrase in lowered)
    if generic:
        warnings.append("可能含通用居中/留白描述：" + "、".join(generic))
    if "\n" in caption or "\r" in caption:
        warnings.append("caption 应为单行")
    return warnings


def validate_caption(caption: str, trigger: str = TRIGGER) -> str:
    normalized = normalize_caption(caption)
    if not normalized.startswith(f"{trigger}."):
        raise ValueError(f"caption 必须以 {trigger}. 开头")
    if len(normalized) <= len(trigger) + 1:
        raise ValueError("caption 正文为空")
    return normalized


def result_schema() -> dict[str, object]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "captions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "sha_id": {"type": "string"},
                        "caption": {"type": "string"},
                        "zh_translation": {"type": "string"},
                    },
                    "required": ["sha_id", "caption", "zh_translation"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["captions"],
        "additionalProperties": False,
    }


def build_prompt(
    items: list[dict[str, object]],
    guidance: str = DEFAULT_GUIDANCE,
    trigger: str = TRIGGER,
) -> str:
    ordered = "\n".join(
        f"{index}. Image #{index}; sha_id={item['id']}"
        for index, item in enumerate(items, start=1)
    )
    return f"""Generate one English LoRA training caption and one faithful Chinese translation for each attached image.

Images are attached in this exact order:
{ordered}

Rules:
- Return exactly one result per image, in the same order, with the exact sha_id shown above.
- Each English caption starts exactly with `{trigger}.` and then uses continuous natural English.
- Prioritize visible details that distinguish this image: subject, appearance, pose, action, clothing, and objects.
- Describe the background once when useful. Mention composition or placement only when it is unusual or important to an action or object relationship.
- Never add a generic closing summary about the subject being centered or surrounded by ample, broad, or generous empty/white space. Do not repeat framing, background, centering, or whitespace to pad the caption.
- Do not guess unclear details or add content merely to reach a word count.
- Keep all artist, art-style, medium, technique, era, aesthetic, and quality language out of the body. The trigger carries the style.
- Aim for concise, specific captions of about 60-120 English words. Longer is acceptable only when the image contains more distinct visible information.
- Append only actually visible markers from: text, watermark, signature, border, speech bubble.
- The Chinese translation must match the English caption and add no information.
- Additional user direction: {guidance.strip() or DEFAULT_GUIDANCE}
- Treat the additional user direction as content guidance for the final English caption. Incorporate the requested visible details, emphasis, or wording directly into that English caption when applicable. The English caption itself must remain English even when the direction is written in Chinese.
- Do not use tools and do not explain your work. Return only the required JSON."""


def validate_model_payload(
    payload: object,
    expected_ids: list[str],
    trigger: str = TRIGGER,
) -> list[dict[str, str]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("captions"), list):
        raise ValueError("Codex 返回值缺少 captions 数组")
    rows = payload["captions"]
    if len(rows) != len(expected_ids):
        raise ValueError(f"Codex 返回 {len(rows)} 条，预期 {len(expected_ids)} 条")
    validated: list[dict[str, str]] = []
    for index, (row, expected_id) in enumerate(zip(rows, expected_ids), start=1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 条不是对象")
        if str(row.get("sha_id", "")) != expected_id:
            raise ValueError(f"第 {index} 条图片 ID 错位")
        caption = validate_caption(str(row.get("caption", "")), trigger)
        translation = normalize_caption(str(row.get("zh_translation", "")))
        if not translation:
            raise ValueError(f"第 {index} 条缺少中文翻译")
        validated.append({"sha_id": expected_id, "caption": caption, "zh_translation": translation})
    return validated


class CaptionerApplication:
    def __init__(
        self,
        selected_dir: Path,
        state_dir: Path,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        concurrency: int = DEFAULT_CONCURRENCY,
        timeout: int = DEFAULT_TIMEOUT,
        provider: AIProvider | None = None,
        trigger: str = TRIGGER,
    ) -> None:
        self.selected_dir = selected_dir.resolve()
        self.state_dir = state_dir.resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        legacy_manifest_path = self.selected_dir / "selection_manifest.csv"
        self.manifest_path = self.state_dir / "selection_manifest.csv"
        if not self.manifest_path.exists() and legacy_manifest_path.is_file():
            shutil.copy2(legacy_manifest_path, self.manifest_path)
        self.state_path = self.state_dir / "caption_state.json"
        self.thumb_dir = self.state_dir / "thumbs"
        self.batch_size = max(1, batch_size)
        self.concurrency = max(1, concurrency)
        self.timeout = max(30, timeout)
        self.provider = provider or CodexCLIProvider()
        self.trigger = trigger
        self.lock = threading.RLock()
        self.thumb_lock = threading.Lock()
        self.worker: threading.Thread | None = None
        self.priority_ids: list[str] = []
        self.run_all = False
        self.pause_requested = False
        self.dataset_error = ""
        self.last_log = ""
        self.guidance = DEFAULT_GUIDANCE

        if not self.selected_dir.is_dir():
            raise ValueError(f"打标目录不存在：{self.selected_dir}")
        if not self.manifest_path.is_file():
            raise ValueError(f"缺少筛选清单：{self.manifest_path}")
        self.manifest_rows, self.manifest_fields = read_csv(self.manifest_path)
        self._validate_manifest_shape()
        self.state = self._load_and_merge_state()
        self._save_state()

    def _validate_manifest_shape(self) -> None:
        if not self.manifest_rows:
            raise ValueError("selection_manifest.csv 为空")
        for required in ("file", "sha256"):
            if required not in self.manifest_fields:
                raise ValueError(f"selection_manifest.csv 缺少 {required} 列")
        ids = [manifest_item_id(row) for row in self.manifest_rows]
        files = [row.get("file", "") for row in self.manifest_rows]
        if any(not value for value in ids) or len(set(ids)) != len(ids):
            raise ValueError("selection_manifest.csv 的 sha256 为空或重复")
        if any(not value for value in files) or len(set(files)) != len(files):
            raise ValueError("selection_manifest.csv 的 file 为空或重复")

    def _load_and_merge_state(self) -> dict[str, object]:
        saved: dict[str, object] = {}
        if self.state_path.exists():
            try:
                loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and loaded.get("version") == STATE_VERSION:
                    saved = loaded
            except (OSError, ValueError, json.JSONDecodeError):
                saved = {}
        saved_items = saved.get("items", {}) if isinstance(saved.get("items"), dict) else {}
        items: dict[str, dict[str, object]] = {}
        order: list[str] = []
        for row in self.manifest_rows:
            item_id = manifest_item_id(row)
            current_file = row["file"]
            original_file = row.get("original_file") or current_file
            prior = saved_items.get(item_id, {}) if isinstance(saved_items, dict) else {}
            prior = dict(prior) if isinstance(prior, dict) else {}
            status = str(prior.get("status", "ungenerated"))
            if status not in STATUSES or status == "generating":
                status = "ungenerated"
            item: dict[str, object] = {
                "id": item_id,
                "index": len(order) + 1,
                "current_file": current_file,
                "original_file": original_file,
                "status": status,
                "caption": str(prior.get("caption", "")),
                "translation": str(prior.get("translation", "")),
                "error": str(prior.get("error", "")),
                "attempts": int(prior.get("attempts", 0) or 0),
                "managed_caption": bool(prior.get("managed_caption", False)),
                "batch_label": str(prior.get("batch_label", "")),
                "generation_started_at": str(prior.get("generation_started_at", "")),
                "generation_completed_at": str(prior.get("generation_completed_at", "")),
                "updated_at": str(prior.get("updated_at", "")),
            }
            image_path = self.selected_dir / current_file
            caption_path = image_path.with_suffix(".txt")
            if caption_path.is_file():
                disk_caption = normalize_caption(caption_path.read_text(encoding="utf-8"))
                if disk_caption and not item["caption"]:
                    item["caption"] = disk_caption
                    item["status"] = "pending"
                    item["managed_caption"] = False
                elif disk_caption and item["caption"] != disk_caption:
                    item["caption"] = disk_caption
                    item["status"] = "pending"
                    item["managed_caption"] = False
                    item["error"] = "检测到外部修改，已从 .txt 重新载入"
            elif status in {"pending", "confirmed"}:
                item["status"] = "failed"
                item["error"] = "同名 caption 文件缺失"
            items[item_id] = item
            order.append(item_id)

        fingerprint = hashlib.sha256("\n".join(order).encode("utf-8")).hexdigest()
        old_fingerprint = str(saved.get("dataset_fingerprint", ""))
        if old_fingerprint and old_fingerprint != fingerprint:
            self.dataset_error = "筛选清单内容发生变化；批处理已暂停，请核对数据集。"
        return {
            "version": STATE_VERSION,
            "updated_at": now_iso(),
            "dataset_fingerprint": fingerprint,
            "queue_status": "paused" if self.dataset_error else "idle",
            "items": items,
            "order": order,
        }

    @property
    def items(self) -> dict[str, dict[str, object]]:
        return self.state["items"]  # type: ignore[return-value]

    @property
    def order(self) -> list[str]:
        return self.state["order"]  # type: ignore[return-value]

    def _save_state(self) -> None:
        self.state["updated_at"] = now_iso()
        self.state["revision"] = uuid.uuid4().hex
        atomic_write_json(self.state_path, self.state)

    def preflight(self, verify_hashes: bool = True) -> dict[str, object]:
        with self.lock:
            expected_files = {str(row["file"]) for row in self.manifest_rows}
            actual_files = {
                path.relative_to(self.selected_dir).as_posix()
                for path in self.selected_dir.iterdir()
                if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
            }
            if expected_files != actual_files:
                missing = sorted(expected_files - actual_files)[:5]
                extra = sorted(actual_files - expected_files)[:5]
                raise ValueError(f"图片与清单不一致；缺少 {missing or '无'}；多出 {extra or '无'}")
            for row in self.manifest_rows:
                image_path = (self.selected_dir / row["file"]).resolve()
                if self.selected_dir not in image_path.parents:
                    raise ValueError(f"不安全的清单路径：{row['file']}")
                if image_path.suffix.lower() not in IMAGE_EXTENSIONS:
                    raise ValueError(f"不支持的图片格式：{row['file']}")
                if verify_hashes and sha256_file(image_path) != row["sha256"]:
                    raise ValueError(f"图片 SHA-256 与清单不一致：{row['file']}")
            return {"count": len(self.manifest_rows), "verified_hashes": verify_hashes}

    def prepare_dataset(self) -> dict[str, object]:
        with self.lock:
            return self.preflight(verify_hashes=True)

    def _caption_path(self, item: dict[str, object]) -> Path:
        return (self.selected_dir / str(item["current_file"])).with_suffix(".txt")

    def _write_generated_caption(self, item: dict[str, object], caption: str, *, force: bool = False) -> None:
        path = self._caption_path(item)
        if path.exists() and not item.get("managed_caption") and not force:
            existing = normalize_caption(path.read_text(encoding="utf-8"))
            if existing != caption:
                raise ValueError(f"已有人工 caption，不会覆盖：{path.name}")
        atomic_write_text(path, caption + "\n")
        item["managed_caption"] = True

    def _run_provider_batch(self, batch: list[dict[str, object]]) -> list[dict[str, str]]:
        expected_ids = [str(item["id"]) for item in batch]
        with self.lock:
            prompt = build_prompt(batch, self.guidance, self.trigger)
        payload = self.provider.generate(
            prompt=prompt,
            image_paths=[self.selected_dir / str(item["current_file"]) for item in batch],
            schema=result_schema(),
            timeout=self.timeout,
        )
        return validate_model_payload(payload, expected_ids, self.trigger)

    def _mark_batch_failed(self, batch: list[dict[str, object]], error: str) -> None:
        with self.lock:
            for item in batch:
                item["status"] = "failed"
                item["error"] = error[-2000:]
                item["updated_at"] = now_iso()
            self.last_log = error[-2000:]
            self._save_state()

    def _process_batch(self, batch_ids: list[str]) -> None:
        with self.lock:
            batch = [self.items[item_id] for item_id in batch_ids]
            for item in batch:
                item["status"] = "generating"
                item["error"] = ""
                item["generation_started_at"] = now_iso()
                item["generation_completed_at"] = ""
                item["updated_at"] = item["generation_started_at"]
            self._save_state()
        last_error = ""
        results: list[dict[str, str]] | None = None
        for attempt in range(MAX_RETRIES + 1):
            with self.lock:
                for item in batch:
                    item["attempts"] = int(item.get("attempts", 0)) + 1
                self._save_state()
            try:
                results = self._run_provider_batch(batch)
                break
            except Exception as exc:  # noqa: BLE001 - worker must turn all failures into item state
                last_error = str(exc)
                if isinstance(exc, InterruptedError):
                    break
                if attempt < MAX_RETRIES:
                    time.sleep(2 + attempt * 3)
        if results is None:
            if self.pause_requested:
                with self.lock:
                    for item in batch:
                        item["status"] = "ungenerated"
                        item["error"] = ""
                        item["updated_at"] = now_iso()
                    resumable = [item_id for item_id in batch_ids if item_id not in self.priority_ids]
                    self.priority_ids = resumable + self.priority_ids
                    self.last_log = "已暂停；当前批次未写入，可继续生成"
                    self._save_state()
                return
            self._mark_batch_failed(batch, last_error or "AI Provider 未返回结果")
            return
        with self.lock:
            by_id = {row["sha_id"]: row for row in results}
            for item in batch:
                result = by_id[str(item["id"])]
                try:
                    force = bool(item.pop("force_overwrite", False))
                    self._write_generated_caption(item, result["caption"], force=force)
                    item["caption"] = result["caption"]
                    item["translation"] = result["zh_translation"]
                    item["status"] = "pending"
                    item["error"] = ""
                except Exception as exc:  # noqa: BLE001
                    item["status"] = "failed"
                    item["error"] = str(exc)
                item["updated_at"] = now_iso()
                item["generation_completed_at"] = item["updated_at"]
            self.last_log = f"完成 {len(batch)} 张图片"
            self._save_state()

    def _next_batch(self) -> list[str]:
        with self.lock:
            chosen: list[str] = []
            while self.priority_ids and len(chosen) < self.batch_size:
                item_id = self.priority_ids.pop(0)
                if item_id in self.items and self.items[item_id]["status"] == "ungenerated":
                    chosen.append(item_id)
            if self.run_all and len(chosen) < self.batch_size:
                for item_id in self.order:
                    if item_id in chosen:
                        continue
                    if self.items[item_id]["status"] == "ungenerated":
                        chosen.append(item_id)
                        if len(chosen) == self.batch_size:
                            break
            return chosen

    def _worker_loop(self) -> None:
        futures: set[Future[None]] = set()
        try:
            with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="caption-batch") as pool:
                while True:
                    with self.lock:
                        paused = self.pause_requested or self.state.get("queue_status") == "paused"
                    while not paused and len(futures) < self.concurrency:
                        batch_ids = self._next_batch()
                        if not batch_ids:
                            break
                        futures.add(pool.submit(self._process_batch, batch_ids))
                    if not futures:
                        with self.lock:
                            if paused:
                                self.state["queue_status"] = "paused"
                            elif self.run_all:
                                failed = any(item["status"] == "failed" for item in self.items.values())
                                self.state["queue_status"] = "complete_with_errors" if failed else "complete"
                            else:
                                self.state["queue_status"] = "idle"
                            self.run_all = False
                            self._save_state()
                        return
                    done, futures = wait(futures, return_when=FIRST_COMPLETED)
                    for future in done:
                        future.result()
                    with self.lock:
                        if self.pause_requested:
                            self.state["queue_status"] = "paused"
                            self.run_all = False
                            self._save_state()
        finally:
            with self.lock:
                self.worker = None

    def _start_worker(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.pause_requested = False
        self.state["queue_status"] = "running"
        self._save_state()
        self.worker = threading.Thread(target=self._worker_loop, name="caption-worker", daemon=True)
        self.worker.start()

    def start_selected_items(
        self,
        item_ids: list[str],
        guidance: str = "",
        batch_label: str = "",
    ) -> dict[str, object]:
        with self.lock:
            if self.dataset_error:
                raise ValueError(self.dataset_error)
            ready, message = self.provider.readiness()
            if not ready:
                raise ValueError(message)
        preflight_result = self.prepare_dataset()
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ValueError("批量打标正在运行，请先暂停")
            valid = list(dict.fromkeys(item_id for item_id in item_ids if item_id in self.items))
            eligible = [item_id for item_id in valid if self.items[item_id]["status"] != "generating"]
            if not eligible:
                raise ValueError("请至少勾选一张可打标图片")
            self.guidance = normalize_caption(guidance) or DEFAULT_GUIDANCE
            normalized_label = normalize_caption(batch_label)
            self.priority_ids = []
            for item_id in eligible:
                item = self.items[item_id]
                if item["status"] in {"pending", "confirmed"}:
                    item["force_overwrite"] = True
                item["status"] = "ungenerated"
                item["error"] = ""
                item["batch_label"] = normalized_label
                self.priority_ids.append(item_id)
            self.run_all = False
            self._start_worker()
            return {
                "queue_status": self.state["queue_status"],
                "preflight": preflight_result,
                "queued": len(eligible),
                "guidance": self.guidance,
                "batch_label": normalized_label,
            }

    def start_all_items(self) -> dict[str, object]:
        with self.lock:
            ids = [item_id for item_id in self.order if self.items[item_id]["status"] == "ungenerated"]
        return self.start_selected_items(ids)

    def pause(self) -> dict[str, object]:
        with self.lock:
            self.pause_requested = True
            self.state["queue_status"] = "pausing"
            self.run_all = False
            self.last_log = "正在立即暂停当前批次…"
            self._save_state()
        self.provider.cancel()
        return {"queue_status": "pausing", "message": "正在暂停；当前批次不会写入，稍后可继续"}

    def resume(self) -> dict[str, object]:
        with self.lock:
            if self.dataset_error:
                raise ValueError(self.dataset_error)
        self.prepare_dataset()
        with self.lock:
            if not self.priority_ids:
                raise ValueError("没有待继续的勾选项，请重新勾选后开始")
            self.run_all = False
            self._start_worker()
            return {"queue_status": self.state["queue_status"]}

    def retry_failed(self) -> dict[str, object]:
        self.prepare_dataset()
        with self.lock:
            failed_ids = [item_id for item_id in self.order if self.items[item_id]["status"] == "failed"]
            for item_id in failed_ids:
                self.items[item_id]["status"] = "ungenerated"
                self.items[item_id]["error"] = ""
            self.priority_ids.extend(item_id for item_id in failed_ids if item_id not in self.priority_ids)
            self.run_all = False
            if failed_ids:
                self._start_worker()
            return {"queued": len(failed_ids), "queue_status": self.state["queue_status"]}

    def generate_ids(
        self,
        item_ids: list[str],
        *,
        force: bool = True,
        guidance: str = "",
    ) -> dict[str, object]:
        self.prepare_dataset()
        with self.lock:
            ready, message = self.provider.readiness()
            if not ready:
                raise ValueError(message)
            valid = [item_id for item_id in item_ids if item_id in self.items]
            if not valid:
                raise ValueError("没有可生成的图片")
            self.guidance = normalize_caption(guidance) or DEFAULT_GUIDANCE
            for item_id in valid:
                item = self.items[item_id]
                item["status"] = "ungenerated"
                item["error"] = ""
                item["force_overwrite"] = force
                if item_id not in self.priority_ids:
                    self.priority_ids.append(item_id)
            if not (self.worker and self.worker.is_alive()):
                self.run_all = False
            self._start_worker()
            return {"queued": len(valid), "queue_status": self.state["queue_status"]}

    def save_caption(self, item_id: str, caption: str, *, confirm: bool) -> dict[str, object]:
        with self.lock:
            item = self.items.get(item_id)
            if not item:
                raise ValueError("图片不存在")
            if item["status"] == "generating":
                raise ValueError("当前图片正在生成中，请等待本批完成后再编辑")
            normalized = validate_caption(caption, self.trigger)
            atomic_write_text(self._caption_path(item), normalized + "\n")
            item["caption"] = normalized
            item["managed_caption"] = True
            item["status"] = "confirmed" if confirm else "pending"
            item["error"] = ""
            item["updated_at"] = now_iso()
            self._save_state()
            return self._public_item(item)

    def _public_item(self, item: dict[str, object]) -> dict[str, object]:
        caption = str(item.get("caption", ""))
        return {
            "id": item["id"],
            "index": item["index"],
            "current_file": item["current_file"],
            "original_file": item["original_file"],
            "status": item["status"],
            "caption": caption,
            "translation": item.get("translation", ""),
            "error": item.get("error", ""),
            "attempts": item.get("attempts", 0),
            "batch_label": item.get("batch_label", ""),
            "generation_started_at": item.get("generation_started_at", ""),
            "generation_completed_at": item.get("generation_completed_at", ""),
            "word_count": word_count(caption, self.trigger),
            "warnings": caption_warnings(caption, self.trigger) if caption else [],
            "updated_at": item.get("updated_at", ""),
        }

    def public_data(self) -> dict[str, object]:
        with self.lock:
            provider_ready, provider_detail = self.provider.readiness()
            public_items = [self._public_item(self.items[item_id]) for item_id in self.order]
            counts = {status: 0 for status in STATUSES}
            for item in public_items:
                counts[str(item["status"])] += 1
            return {
                "selected_dir": str(self.selected_dir),
                "revision": self.state.get("revision", ""),
                "queue_status": self.state.get("queue_status", "idle"),
                "dataset_error": self.dataset_error,
                "last_log": self.last_log,
                "guidance": self.guidance,
                "model": self.provider.model_label,
                "provider": {
                    "id": self.provider.provider_id,
                    "name": self.provider.display_name,
                    "model": self.provider.model_label,
                    "ready": provider_ready,
                    "detail": provider_detail,
                },
                "batch_size": self.batch_size,
                "concurrency": self.concurrency,
                "trigger": self.trigger,
                "counts": counts,
                "items": public_items,
            }

    def image_path(self, item_id: str) -> Path:
        item = self.items.get(item_id)
        if not item:
            raise FileNotFoundError(item_id)
        path = (self.selected_dir / str(item["current_file"])).resolve()
        if self.selected_dir not in path.parents or not path.is_file():
            raise FileNotFoundError(path)
        return path

    def thumbnail_path(self, item_id: str) -> Path:
        target = self.thumb_dir / f"{item_id}.jpg"
        if target.is_file():
            return target
        with self.thumb_lock:
            if target.is_file():
                return target
            source = self.image_path(item_id)
            try:
                with Image.open(source) as opened:
                    image = ImageOps.exif_transpose(opened).convert("RGB")
                    image.thumbnail((420, 420), Image.Resampling.LANCZOS)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                    image.save(temporary, format="JPEG", quality=86, optimize=True)
                    os.replace(temporary, target)
            except (OSError, UnidentifiedImageError) as exc:
                raise FileNotFoundError(str(exc)) from exc
        return target


def choose_image_directory() -> Path | None:
    """Open the platform folder picker without exposing filesystem paths to browser JavaScript."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(title="选择包含训练图片的文件夹", mustexist=True)
        root.destroy()
        return Path(selected).expanduser().resolve() if selected else None
    except Exception as tkinter_error:  # noqa: BLE001 - platform UI availability varies
        if sys.platform == "darwin" and shutil.which("osascript"):
            completed = subprocess.run(
                ["osascript", "-e", 'POSIX path of (choose folder with prompt "选择包含训练图片的文件夹")'],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode == 0 and completed.stdout.strip():
                return Path(completed.stdout.strip()).expanduser().resolve()
            if "User canceled" in completed.stderr or "-128" in completed.stderr:
                return None
        raise RuntimeError("无法打开系统文件夹选择器；也可以在启动命令中直接传入图片目录") from tkinter_error


class CaptionerManager:
    def __init__(
        self,
        selected_dir: Path | None = None,
        state_dir: Path | None = None,
        *,
        state_root: Path = DEFAULT_STATE_ROOT,
        batch_size: int = DEFAULT_BATCH_SIZE,
        concurrency: int = DEFAULT_CONCURRENCY,
        timeout: int = DEFAULT_TIMEOUT,
        settings_store: SettingsStore | None = None,
        provider: AIProvider | None = None,
    ) -> None:
        self.lock = threading.RLock()
        self.state_root = state_root.expanduser().resolve()
        self.settings_path = self.state_root / "settings.json"
        self.batch_size = batch_size
        self.concurrency = concurrency
        self.timeout = timeout
        self.settings = settings_store or SettingsStore()
        self.provider = provider or create_provider(self.settings)
        self.application: CaptionerApplication | None = None
        if selected_dir is not None:
            self._load(selected_dir, state_dir)
        elif self.settings_path.is_file():
            try:
                settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
                last_selected = Path(str(settings.get("last_selected_dir", ""))).expanduser()
                if last_selected.is_dir():
                    self._load(last_selected, None)
            except (OSError, ValueError, json.JSONDecodeError):
                self.application = None

    def _load(self, selected_dir: Path, state_dir: Path | None) -> CaptionerApplication:
        selected = selected_dir.expanduser().resolve()
        if not selected.is_dir():
            raise ValueError(f"打标目录不存在：{selected}")
        resolved_state = state_dir.expanduser().resolve() if state_dir else default_state_dir(selected, self.state_root)
        ensure_manifest(selected, resolved_state)
        candidate = CaptionerApplication(
            selected,
            resolved_state,
            batch_size=self.batch_size,
            concurrency=self.concurrency,
            timeout=self.timeout,
            provider=self.provider,
            trigger=self.settings.trigger,
        )
        with self.lock:
            self.application = candidate
            self.state_root.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.settings_path, {"last_selected_dir": str(selected)})
        return candidate

    def current(self) -> CaptionerApplication:
        with self.lock:
            if self.application is None:
                raise ValueError("请先选择包含训练图片的文件夹")
            return self.application

    def select_directory(self, selected_dir: Path | None = None) -> dict[str, object]:
        with self.lock:
            current = self.application
            if current and current.state.get("queue_status") in {"running", "pausing"}:
                raise ValueError("请先暂停当前生成任务，再切换文件夹")
        selected = selected_dir or choose_image_directory()
        if selected is None:
            return {"cancelled": True}
        self._load(selected, None)
        return {"cancelled": False, "data": self.public_data()}

    def update_settings(self, payload: dict[str, object]) -> dict[str, object]:
        with self.lock:
            if self.application and self.application.state.get("queue_status") in {"running", "pausing"}:
                raise ValueError("请先暂停当前生成任务，再修改 AI 设置")
            self.settings.update(payload)
            self.provider = create_provider(self.settings)
            if self.application:
                self.application.provider = self.provider
                self.application.trigger = self.settings.trigger
                self.application._save_state()
        return self.public_data()

    def public_data(self) -> dict[str, object]:
        with self.lock:
            application = self.application
        if application:
            data = application.public_data()
            data["settings"] = self.settings.public()
            return data
        provider_ready, provider_detail = self.provider.readiness()
        return {
            "selected_dir": "",
            "revision": "empty",
            "queue_status": "idle",
            "dataset_error": "",
            "last_log": "请选择一个图片文件夹开始",
            "guidance": DEFAULT_GUIDANCE,
            "model": self.provider.model_label,
            "provider": {
                "id": self.provider.provider_id,
                "name": self.provider.display_name,
                "model": self.provider.model_label,
                "ready": provider_ready,
                "detail": provider_detail,
            },
            "settings": self.settings.public(),
            "batch_size": self.batch_size,
            "concurrency": self.concurrency,
            "trigger": self.settings.trigger,
            "counts": {status: 0 for status in STATUSES},
            "items": [],
        }


def make_handler(application: CaptionerApplication | CaptionerManager) -> type[BaseHTTPRequestHandler]:
    ui_dir = Path(__file__).with_name("ui")

    def current_application() -> CaptionerApplication:
        return application.current() if isinstance(application, CaptionerManager) else application

    def public_data() -> dict[str, object]:
        return application.public_data()

    class CaptionerHandler(BaseHTTPRequestHandler):
        server_version = "LoRACaptioner/1.0"

        def log_message(self, format_string: str, *args: object) -> None:
            if self.path.startswith("/api/") and not self.path.startswith("/api/data"):
                super().log_message(format_string, *args)

        def send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_file(self, path: Path, content_type: str, *, cache: bool = False) -> None:
            if not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=31536000" if cache else "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = unquote(urlparse(self.path).path)
            if path == "/api/data":
                self.send_json(public_data())
                return
            if path.startswith("/thumb/"):
                item_id = path.removeprefix("/thumb/").removesuffix(".jpg")
                try:
                    self.send_file(current_application().thumbnail_path(item_id), "image/jpeg", cache=True)
                except (FileNotFoundError, ValueError):
                    self.send_error(HTTPStatus.NOT_FOUND)
                return
            if path.startswith("/original/"):
                item_id = path.removeprefix("/original/")
                try:
                    original = current_application().image_path(item_id)
                    self.send_file(original, mimetypes.guess_type(original.name)[0] or "application/octet-stream")
                except (FileNotFoundError, ValueError):
                    self.send_error(HTTPStatus.NOT_FOUND)
                return
            static_files = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            }
            if path in static_files:
                filename, content_type = static_files[path]
                self.send_file(ui_dir / filename, content_type)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 2_000_000:
                    raise ValueError("请求过大")
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("请求正文必须是 JSON 对象")
                path = urlparse(self.path).path
                if path == "/api/dataset/select":
                    if not isinstance(application, CaptionerManager):
                        raise ValueError("当前服务不支持切换文件夹")
                    requested = str(body.get("path", "")).strip()
                    self.send_json(application.select_directory(Path(requested) if requested else None))
                elif path == "/api/start":
                    ids = body.get("ids", [])
                    if not isinstance(ids, list):
                        raise ValueError("ids 必须是数组")
                    self.send_json(
                        current_application().start_selected_items(
                            [str(item_id) for item_id in ids],
                            str(body.get("guidance", "")),
                            str(body.get("batch_label", "")),
                        )
                    )
                elif path == "/api/pause":
                    self.send_json(current_application().pause())
                elif path == "/api/resume":
                    self.send_json(current_application().resume())
                elif path == "/api/settings":
                    if not isinstance(application, CaptionerManager):
                        raise ValueError("当前服务不支持修改 AI 设置")
                    self.send_json(application.update_settings(body))
                elif path == "/api/retry":
                    self.send_json(current_application().retry_failed())
                elif path == "/api/generate":
                    ids = body.get("ids", [])
                    if not isinstance(ids, list):
                        raise ValueError("ids 必须是数组")
                    self.send_json(
                        current_application().generate_ids(
                            [str(item_id) for item_id in ids],
                            force=True,
                            guidance=str(body.get("guidance", "")),
                        )
                    )
                elif path in {"/api/caption/save", "/api/caption/confirm"}:
                    self.send_json(
                        current_application().save_caption(
                            str(body.get("id", "")),
                            str(body.get("caption", "")),
                            confirm=path.endswith("confirm"),
                        )
                    )
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    return CaptionerHandler


def bind_server(host: str, port: int, handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    try:
        return ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        if exc.errno not in {48, 98, 10048}:
            raise
        return ThreadingHTTPServer((host, 0), handler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="跨平台 LoRA 图片批量打标与复核工具")
    parser.add_argument("selected_dir", type=Path, nargs="?", help="可选的初始图片目录；也可在页面中选择")
    parser.add_argument("--state-dir", type=Path, help="仅用于初始目录的打标状态和缩略图目录")
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT, help="通过页面选择的各数据集状态根目录")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--no-open", action="store_true")
    return parser.parse_args()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    if sys.version_info < (3, 10):
        print("LoRA Caption Studio 需要 Python 3.10 或更高版本。", file=sys.stderr)
        return 2
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).parents[1] / ".env", override=False)
    except ImportError:
        pass
    args = parse_args()
    selected_dir = args.selected_dir.expanduser().resolve() if args.selected_dir else None
    state_dir = args.state_dir.expanduser().resolve() if args.state_dir else None
    if state_dir and selected_dir is None:
        print("--state-dir 需要同时提供初始图片目录。", file=sys.stderr)
        return 2
    try:
        application = CaptionerManager(
            selected_dir,
            state_dir,
            state_root=args.state_root,
            batch_size=args.batch_size,
            concurrency=args.concurrency,
            timeout=args.timeout,
        )
        server = bind_server(args.host, args.port, make_handler(application))
    except (OSError, ValueError) as exc:
        print(f"无法启动打标器：{exc}", file=sys.stderr)
        return 2
    url = f"http://{args.host}:{server.server_port}/"
    print(f"\nLoRA 打标器已启动：{url}")
    print("可在页面中选择任意图片文件夹；按 Ctrl+C 停止。", flush=True)
    if not args.no_open:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n打标器已停止。")
    finally:
        if application.application:
            application.application.provider.cancel()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
