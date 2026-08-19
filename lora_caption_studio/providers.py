"""AI provider interface and built-in Codex CLI / OpenAI implementations."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

from .config import SettingsStore


CODEX_INSTALL_URL = "https://developers.openai.com/codex/cli"


class ProviderError(RuntimeError):
    """An actionable provider configuration or generation error."""


def find_codex_cli() -> str | None:
    """Find a standalone user-installed CLI and reject desktop-app internals."""
    candidate = shutil.which("codex")
    if not candidate:
        return None
    resolved = Path(candidate).expanduser().resolve()
    lowered_parts = [part.casefold() for part in resolved.parts]
    inside_macos_app = any(part.endswith(".app") for part in lowered_parts) and "contents" in lowered_parts
    inside_windows_app = "windowsapps" in lowered_parts and any(
        "chatgpt" in part or "openai" in part for part in lowered_parts
    )
    if inside_macos_app or inside_windows_app:
        return None
    return str(resolved)


class AIProvider(ABC):
    provider_id: str
    display_name: str

    @property
    @abstractmethod
    def model_label(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def readiness(self) -> tuple[bool, str]:
        raise NotImplementedError

    @abstractmethod
    def generate(
        self,
        *,
        prompt: str,
        image_paths: list[Path],
        schema: dict[str, object],
        timeout: int,
    ) -> object:
        raise NotImplementedError

    def cancel(self) -> None:
        """Cancel in-flight work when the provider supports it."""


def _process_options() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate_process(process: subprocess.Popen[str]) -> None:
    """Terminate one provider subprocess without relying on POSIX-only APIs."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


class CodexCLIProvider(AIProvider):
    provider_id = "codex"
    display_name = "Codex CLI"

    def __init__(
        self,
        *,
        model: str = "",
        binary: str | None = None,
        command_runner: Callable[[list[str], int, Path], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        # `binary` and `command_runner` exist for isolated tests. Production discovery is PATH-only.
        self.binary = binary or find_codex_cli()
        self.model = model.strip()
        self.command_runner = command_runner
        self._lock = threading.Lock()
        self._active: set[subprocess.Popen[str]] = set()
        self._cancelled = threading.Event()

    @property
    def model_label(self) -> str:
        return self.model or "CLI 默认模型"

    def readiness(self) -> tuple[bool, str]:
        if self.binary:
            return True, "已检测到系统 PATH 中的 Codex CLI"
        return False, f"未检测到 Codex CLI。请按官方文档安装，然后在终端运行 codex 完成登录：{CODEX_INSTALL_URL}"

    def _run(self, command: list[str], timeout: int, cwd: Path) -> subprocess.CompletedProcess[str]:
        if self.command_runner:
            return self.command_runner(command, timeout, cwd)
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_process_options(),
        )
        with self._lock:
            self._active.add(process)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            terminate_process(process)
            process.communicate()
            raise TimeoutError(f"Codex CLI 超过 {timeout // 60} 分钟未完成") from exc
        finally:
            with self._lock:
                self._active.discard(process)
        if self._cancelled.is_set():
            raise InterruptedError("当前批次已暂停")
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def generate(
        self,
        *,
        prompt: str,
        image_paths: list[Path],
        schema: dict[str, object],
        timeout: int,
    ) -> object:
        ready, message = self.readiness()
        if not ready or not self.binary:
            raise ProviderError(message)
        self._cancelled.clear()
        with tempfile.TemporaryDirectory(prefix="lora-caption-") as temporary_text:
            temporary = Path(temporary_text)
            schema_path = temporary / "schema.json"
            output_path = temporary / "result.json"
            schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
            command = [
                self.binary,
                "exec",
                prompt,
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
            ]
            if self.model:
                command.extend(["--model", self.model])
            for path in image_paths:
                command.extend(["--image", str(path)])
            completed = self._run(command, timeout, image_paths[0].parent)
            if completed.returncode != 0:
                error = (completed.stderr or completed.stdout or "Codex CLI 执行失败").strip()
                raise ProviderError(
                    f"{error[-1800:]}\n如果尚未登录，请先在终端运行 codex 并按提示登录。"
                )
            raw = output_path.read_text(encoding="utf-8") if output_path.exists() else completed.stdout
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ProviderError(f"Codex CLI 返回的不是合法 JSON：{exc}") from exc

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            processes = list(self._active)
        for process in processes:
            terminate_process(process)


class OpenAIAPIProvider(AIProvider):
    provider_id = "openai"
    display_name = "OpenAI API"

    def __init__(self, *, api_key: str, model: str) -> None:
        self.api_key = api_key.strip()
        self.model = model.strip()
        self._cancelled = threading.Event()

    @property
    def model_label(self) -> str:
        return self.model

    def readiness(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "请填写 OpenAI API Key，或设置 OPENAI_API_KEY 环境变量"
        if not self.model:
            return False, "请选择 OpenAI 模型"
        return True, "OpenAI API 已配置"

    @staticmethod
    def _image_data_url(path: Path) -> str:
        mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    def generate(
        self,
        *,
        prompt: str,
        image_paths: list[Path],
        schema: dict[str, object],
        timeout: int,
    ) -> object:
        ready, message = self.readiness()
        if not ready:
            raise ProviderError(message)
        self._cancelled.clear()
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ProviderError("缺少 openai 依赖，请重新安装 requirements.txt") from exc

        content: list[dict[str, object]] = [{"type": "input_text", "text": prompt}]
        content.extend(
            {"type": "input_image", "image_url": self._image_data_url(path), "detail": "high"}
            for path in image_paths
        )
        try:
            client = OpenAI(api_key=self.api_key, timeout=timeout, max_retries=0)
            response = client.responses.create(
                model=self.model,
                input=[{"role": "user", "content": content}],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "caption_batch",
                        "schema": schema,
                        "strict": True,
                    }
                },
            )
        except Exception as exc:  # SDK error types vary by release
            raise ProviderError(f"OpenAI API 请求失败：{exc}") from exc
        if self._cancelled.is_set():
            raise InterruptedError("当前批次已暂停")
        try:
            return json.loads(response.output_text)
        except (AttributeError, json.JSONDecodeError) as exc:
            raise ProviderError("OpenAI API 未返回可解析的结构化 Caption") from exc

    def cancel(self) -> None:
        # Responses requests cannot always be interrupted mid-flight; the result is discarded on return.
        self._cancelled.set()


def create_provider(settings: SettingsStore) -> AIProvider:
    if settings.provider_id == "openai":
        return OpenAIAPIProvider(api_key=settings.api_key(), model=settings.openai_model)
    return CodexCLIProvider(model=settings.codex_model)
