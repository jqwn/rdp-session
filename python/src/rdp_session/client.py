from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union

DEFAULT_BINARY_ENV = "RDP_SESSION_BIN"
DEFAULT_PASSWORD_ENV = "RDP_PASSWORD"
GITHUB_RELEASE_BASE_URL = "https://github.com/jqwn/rdp-session/releases/download"


class RdpSessionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        exit_code: Optional[int] = None,
        kind: Optional[str] = None,
        stdout: str = "",
        stderr: str = "",
        command: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.kind = kind
        self.stdout = stdout
        self.stderr = stderr
        self.command = tuple(command)


@dataclass(frozen=True)
class CreateSessionReport:
    host: str
    port: int
    username: str
    domain: Optional[str]
    desktop_width: int
    desktop_height: int
    negotiated_width: int
    negotiated_height: int
    compression_type: str
    active_frames: int
    graphics_updates: int
    response_frames: int
    terminated_by_server: bool
    dismiss_action: str
    dismiss_sent: bool
    screenshot_path: Optional[str]
    screenshot_saved: bool
    detached: bool
    raw: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "CreateSessionReport":
        return cls(
            host=str(data["host"]),
            port=int(data["port"]),
            username=str(data["username"]),
            domain=_optional_str(data.get("domain")),
            desktop_width=int(data["desktop_width"]),
            desktop_height=int(data["desktop_height"]),
            negotiated_width=int(data["negotiated_width"]),
            negotiated_height=int(data["negotiated_height"]),
            compression_type=str(data["compression_type"]),
            active_frames=int(data["active_frames"]),
            graphics_updates=int(data["graphics_updates"]),
            response_frames=int(data["response_frames"]),
            terminated_by_server=bool(data["terminated_by_server"]),
            dismiss_action=str(data["dismiss_action"]),
            dismiss_sent=bool(data["dismiss_sent"]),
            screenshot_path=_optional_str(data.get("screenshot_path")),
            screenshot_saved=bool(data["screenshot_saved"]),
            detached=bool(data["detached"]),
            raw=dict(data),
        )


def create_session(
    *,
    username: str,
    host: str = "127.0.0.1",
    port: int = 3389,
    domain: Optional[str] = None,
    password: Optional[str] = None,
    password_env: str = DEFAULT_PASSWORD_ENV,
    allow_insecure_cert: bool = False,
    screenshot: Optional[Union[os.PathLike[str], str]] = None,
    dismiss_action: str = "none",
    desktop_width: Optional[int] = None,
    desktop_height: Optional[int] = None,
    connect_timeout_seconds: Optional[int] = None,
    read_timeout_seconds: Optional[int] = None,
    operation_timeout_seconds: Optional[int] = None,
    active_idle_seconds: Optional[int] = None,
    min_active_frames: Optional[int] = None,
    min_graphics_updates: Optional[int] = None,
    tool: Optional[Union[os.PathLike[str], str]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> CreateSessionReport:
    """Create an RDP session by invoking the Rust CLI.

    If ``password`` is provided, it is sent to the child process through stdin.
    Otherwise the CLI reads the environment variable named by ``password_env``.
    Values in ``env`` are merged over the current process environment.
    """

    process_env = _build_process_env(env)
    command = [
        _resolve_tool(tool, process_env),
        "--output",
        "json",
        "create",
        "--host",
        host,
        "--port",
        str(port),
        "--username",
        username,
        "--dismiss-action",
        dismiss_action,
    ]

    stdin_text = None
    if password is None:
        command.extend(["--password-env", password_env])
    else:
        command.append("--password-stdin")
        stdin_text = password

    if domain is not None:
        command.extend(["--domain", domain])
    if allow_insecure_cert:
        command.append("--allow-insecure-cert")
    if screenshot is not None:
        command.extend(["--screenshot", os.fspath(screenshot)])

    _append_optional_int(command, "--desktop-width", desktop_width)
    _append_optional_int(command, "--desktop-height", desktop_height)
    _append_optional_int(command, "--connect-timeout-seconds", connect_timeout_seconds)
    _append_optional_int(command, "--read-timeout-seconds", read_timeout_seconds)
    _append_optional_int(command, "--operation-timeout-seconds", operation_timeout_seconds)
    _append_optional_int(command, "--active-idle-seconds", active_idle_seconds)
    _append_optional_int(command, "--min-active-frames", min_active_frames)
    _append_optional_int(command, "--min-graphics-updates", min_graphics_updates)

    completed = _run(command, stdin_text, process_env)
    if completed.returncode != 0:
        _raise_completed_error(completed, command)

    try:
        payload = json.loads(completed.stdout)
        return CreateSessionReport.from_mapping(payload)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RdpSessionError(
            f"rdp-session returned invalid JSON: {exc}",
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            command=command,
        ) from exc


def _run(
    command: Sequence[str],
    stdin_text: Optional[str],
    env: Optional[Mapping[str, str]],
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            text=True,
            input=stdin_text,
            capture_output=True,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RdpSessionError(
            f"rdp-session binary was not found: {command[0]}",
            command=command,
        ) from exc


def _resolve_tool(
    tool: Optional[Union[os.PathLike[str], str]],
    env: Optional[Mapping[str, str]],
) -> str:
    if tool is not None:
        return os.fspath(tool)

    source_env = env if env is not None else os.environ
    env_tool = source_env.get(DEFAULT_BINARY_ENV)
    if env_tool:
        return env_tool

    binary_name = "rdp-session.exe" if platform.system() == "Windows" else "rdp-session"
    resolved = shutil.which(binary_name, path=source_env.get("PATH"))
    if resolved:
        return resolved

    if platform.system() == "Windows":
        return str(_ensure_downloaded_windows_tool(source_env))

    return binary_name


def _build_process_env(env: Optional[Mapping[str, str]]) -> Optional[Mapping[str, str]]:
    if env is None:
        return None

    merged = os.environ.copy()
    merged.update(env)
    return merged


def _ensure_downloaded_windows_tool(env: Mapping[str, str]) -> Path:
    version = _package_version()
    tag = _release_tag(version)
    arch = _windows_asset_arch()
    asset_name = f"rdp-session-{tag}-windows-{arch}.exe"
    target = _download_cache_dir(env, tag) / asset_name

    if target.exists():
        return target

    url = f"{GITHUB_RELEASE_BASE_URL}/{tag}/{asset_name}"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp")

    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            status = getattr(response, "status", 200)
            if status >= 400:
                raise OSError(f"HTTP {status}")
            with temporary.open("wb") as output:
                shutil.copyfileobj(response, output)
        temporary.replace(target)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise RdpSessionError(
            "could not download rdp-session binary from "
            f"{url}; pass tool=... or set {DEFAULT_BINARY_ENV} to use a local binary"
        ) from exc

    return target


def _package_version() -> str:
    try:
        return importlib.metadata.version("rdp-session")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RdpSessionError(
            "could not determine the rdp-session package version for automatic "
            f"binary download; pass tool=... or set {DEFAULT_BINARY_ENV}"
        ) from exc


def _release_tag(version: str) -> str:
    if version.startswith("v"):
        return version
    return f"v{version}"


def _windows_asset_arch() -> str:
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        return "x86_64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"

    raise RdpSessionError(
        "automatic rdp-session binary download is not available for "
        f"Windows architecture {platform.machine()!r}; pass tool=... or set {DEFAULT_BINARY_ENV}"
    )


def _download_cache_dir(env: Mapping[str, str], tag: str) -> Path:
    local_app_data = env.get("LOCALAPPDATA")
    if local_app_data:
        root = Path(local_app_data)
    else:
        root = Path.home() / "AppData" / "Local"

    return root / "rdp-session" / "bin" / tag


def _raise_completed_error(
    completed: subprocess.CompletedProcess[str],
    command: Sequence[str],
) -> None:
    error_payload = _last_json_line(completed.stderr)
    if error_payload:
        message = str(error_payload.get("error") or "rdp-session failed")
        kind = _optional_str(error_payload.get("kind"))
        exit_code = _optional_int(error_payload.get("exit_code")) or completed.returncode
    else:
        message = completed.stderr.strip() or f"rdp-session exited with {completed.returncode}"
        kind = None
        exit_code = completed.returncode

    raise RdpSessionError(
        message,
        exit_code=exit_code,
        kind=kind,
        stdout=completed.stdout,
        stderr=completed.stderr,
        command=command,
    )


def _append_optional_int(command: list[str], option: str, value: Optional[int]) -> None:
    if value is not None:
        command.extend([option, str(value)])


def _last_json_line(text: str) -> Optional[Mapping[str, Any]]:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
