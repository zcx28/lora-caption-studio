#!/usr/bin/env python3
"""Cross-platform environment bootstrapper used by the macOS and Windows launchers."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import venv
from pathlib import Path

from lora_caption_studio.providers import find_codex_cli


MINIMUM_PYTHON = (3, 10)
PROJECT_ROOT = Path(__file__).resolve().parent
VENV_DIR = PROJECT_ROOT / ".venv"
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def requirements_fingerprint() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def ensure_environment() -> Path:
    if sys.version_info < MINIMUM_PYTHON:
        version = ".".join(map(str, MINIMUM_PYTHON))
        raise RuntimeError(f"需要 Python {version} 或更高版本；当前是 {sys.version.split()[0]}")
    print(f"[检查] Python {sys.version.split()[0]}: {sys.executable}", flush=True)

    python = venv_python()
    if not python.is_file():
        print("[安装] 正在创建项目虚拟环境 .venv …", flush=True)
        venv.EnvBuilder(with_pip=True).create(VENV_DIR)

    marker = VENV_DIR / ".requirements.sha256"
    expected = requirements_fingerprint()
    installed = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
    if installed != expected:
        print("[安装] 正在安装或更新 Python 依赖 …", flush=True)
        completed = subprocess.run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)],
            cwd=PROJECT_ROOT,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("依赖安装失败，请检查网络后重试，或按 README 手动安装")
        marker.write_text(expected + "\n", encoding="utf-8")

    codex = find_codex_cli()
    if codex:
        print(f"[检查] Codex CLI: {codex}", flush=True)
    else:
        print("[提示] 未检测到独立安装的 Codex CLI；程序仍会启动，你可以在页面中改用 OpenAI API。", flush=True)
    return python


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="LoRA Caption Studio 环境检查与启动器")
    parser.add_argument("--check", action="store_true", help="只检查并安装依赖，不启动网页服务")
    args, app_args = parser.parse_known_args()
    try:
        python = ensure_environment()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"\n启动失败：{exc}", file=sys.stderr)
        return 1
    if args.check:
        completed = subprocess.run(
            [str(python), "-c", "import PIL, openai, dotenv, lora_caption_studio; print('[完成] 环境检查通过')"],
            cwd=PROJECT_ROOT,
            check=False,
        )
        return completed.returncode
    return subprocess.call([str(python), "-m", "lora_caption_studio", *app_args], cwd=PROJECT_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
