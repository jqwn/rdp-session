from __future__ import annotations

import importlib.metadata
import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union

DEFAULT_BINARY_ENV = "RDP_SESSION_BIN"
DEFAULT_PASSWORD_ENV = "RDP_PASSWORD"
GITHUB_RELEASE_BASE_URL = "https://github.com/jqwn/rdp-session/releases/download"
STABLE_VERSION_RE = re.compile(r"^[0-9]+[.][0-9]+[.][0-9]+$")


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
            host=_required_str(data, "host"),
            port=_required_int(data, "port"),
            username=_required_str(data, "username"),
            domain=_optional_str(data, "domain"),
            desktop_width=_required_int(data, "desktop_width"),
            desktop_height=_required_int(data, "desktop_height"),
            negotiated_width=_required_int(data, "negotiated_width"),
            negotiated_height=_required_int(data, "negotiated_height"),
            compression_type=_required_str(data, "compression_type"),
            active_frames=_required_int(data, "active_frames"),
            graphics_updates=_required_int(data, "graphics_updates"),
            response_frames=_required_int(data, "response_frames"),
            terminated_by_server=_required_bool(data, "terminated_by_server"),
            dismiss_action=_required_str(data, "dismiss_action"),
            dismiss_sent=_required_bool(data, "dismiss_sent"),
            screenshot_path=_optional_str(data, "screenshot_path"),
            screenshot_saved=_required_bool(data, "screenshot_saved"),
            detached=_required_bool(data, "detached"),
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

    stdin_bytes = None
    if password is None:
        command.extend(["--password-env", password_env])
    else:
        command.append("--password-stdin")
        stdin_bytes = password.encode("utf-8")

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

    completed = _run(command, stdin_bytes, process_env)
    if completed.returncode != 0:
        _raise_completed_error(completed, command)

    try:
        payload = json.loads(completed.stdout)
        return CreateSessionReport.from_mapping(payload)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RdpSessionError(
            f"invalid rdp-session response: {exc}",
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            command=command,
        ) from exc


def _run(
    command: Sequence[str],
    stdin_bytes: Optional[bytes],
    env: Optional[Mapping[str, str]],
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            input=stdin_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        return subprocess.CompletedProcess(
            args=completed.args,
            returncode=completed.returncode,
            stdout=completed.stdout.decode("utf-8", errors="strict"),
            stderr=completed.stderr.decode("utf-8", errors="strict"),
        )
    except UnicodeDecodeError as exc:
        raise RdpSessionError(
            "rdp-session returned output that is not valid UTF-8",
            command=command,
        ) from exc
    except FileNotFoundError as exc:
        raise RdpSessionError(
            f"rdp-session binary was not found: {command[0]}",
            command=command,
        ) from exc
    except OSError as exc:
        raise RdpSessionError(
            f"could not start rdp-session binary {command[0]!r}: {exc}",
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
    expected_sha256 = _download_sha256(tag, asset_name)
    target = _download_cache_dir(env, tag) / asset_name

    if target.exists():
        try:
            if not target.is_file():
                raise RdpSessionError(
                    f"cached rdp-session binary is not a regular file: {target}"
                )
            if _sha256_file(target) == expected_sha256:
                return target
            target.unlink()
        except RdpSessionError:
            raise
        except OSError as exc:
            raise RdpSessionError(
                f"could not verify cached rdp-session binary {target}"
            ) from exc

    url = f"{GITHUB_RELEASE_BASE_URL}/{tag}/{asset_name}"
    temporary = None

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "wb",
            delete=False,
            dir=target.parent,
            prefix=f"{target.name}.",
            suffix=".tmp",
        )
        temporary = Path(handle.name)
        with urllib.request.urlopen(url, timeout=60) as response:
            status = getattr(response, "status", 200)
            if status >= 400:
                raise OSError(f"HTTP {status}")
            with handle as output:
                shutil.copyfileobj(response, output)

        if _sha256_file(temporary) != expected_sha256:
            raise OSError("downloaded binary checksum did not match release checksum")

        os.replace(temporary, target)
        temporary = None
    except Exception as exc:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()
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
    if not STABLE_VERSION_RE.fullmatch(version):
        raise RdpSessionError(
            "automatic rdp-session binary download requires an installed stable "
            f"version like 1.2.3, got {version!r}; pass tool=... or set {DEFAULT_BINARY_ENV}"
        )
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


def _download_sha256(tag: str, asset_name: str) -> str:
    checksum_name = f"{asset_name}.sha256"
    url = f"{GITHUB_RELEASE_BASE_URL}/{tag}/{checksum_name}"

    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            status = getattr(response, "status", 200)
            if status >= 400:
                raise OSError(f"HTTP {status}")
            text = response.read().decode("ascii", errors="strict")
    except Exception as exc:
        raise RdpSessionError(
            "could not download rdp-session checksum from "
            f"{url}; pass tool=... or set {DEFAULT_BINARY_ENV} to use a local binary"
        ) from exc

    token = text.split()[0] if text.split() else ""
    if len(token) != 64 or not all(char in "0123456789abcdefABCDEF" for char in token):
        raise RdpSessionError(f"invalid rdp-session checksum file from {url}")

    return token.lower()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _raise_completed_error(
    completed: subprocess.CompletedProcess[str],
    command: Sequence[str],
) -> None:
    error_payload = _last_json_line(completed.stderr)
    if error_payload:
        message = str(error_payload.get("error") or "rdp-session failed")
        kind = _optional_value_str(error_payload.get("kind"))
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


def _required_str(data: Mapping[str, Any], field: str) -> str:
    value = data[field]
    if type(value) is not str:
        raise TypeError(f"{field} must be a string")
    return value


def _required_int(data: Mapping[str, Any], field: str) -> int:
    value = data[field]
    if type(value) is not int:
        raise TypeError(f"{field} must be an integer")
    return value


def _required_bool(data: Mapping[str, Any], field: str) -> bool:
    value = data[field]
    if type(value) is not bool:
        raise TypeError(f"{field} must be a boolean")
    return value


def _optional_str(data: Mapping[str, Any], field: str) -> Optional[str]:
    value = data[field]
    if value is None:
        return None
    if type(value) is not str:
        raise TypeError(f"{field} must be null or a string")
    return value


def _optional_value_str(value: Any) -> Optional[str]:
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
