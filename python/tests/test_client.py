import concurrent.futures
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import call, patch

if os.environ.get("RDP_SESSION_TEST_INSTALLED") != "1":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rdp_session import RdpSessionError, create_session


REPORT = {
    "host": "127.0.0.1",
    "port": 3389,
    "username": "appuser",
    "domain": None,
    "desktop_width": 1280,
    "desktop_height": 1024,
    "negotiated_width": 1280,
    "negotiated_height": 1024,
    "compression_type": "Rdp61",
    "active_frames": 2,
    "graphics_updates": 1,
    "response_frames": 3,
    "terminated_by_server": False,
    "dismiss_action": "none",
    "dismiss_sent": False,
    "screenshot_path": None,
    "screenshot_saved": False,
    "detached": True,
}


def completed(stdout=None, stderr=b"", returncode=0):
    if stdout is None:
        stdout = json.dumps(REPORT).encode("utf-8")
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def response(body):
    result = io.BytesIO(body)
    result.status = 200
    result.__enter__ = lambda: result
    result.__exit__ = lambda *args: None
    return result


def sha256_text(body, filename="asset.exe"):
    return f"{hashlib.sha256(body).hexdigest()}  {filename}\n".encode("ascii")


class CreateSessionTests(unittest.TestCase):
    @patch("rdp_session.client.subprocess.run")
    def test_uses_password_env_by_default(self, run):
        run.return_value = completed()

        report = create_session(username="appuser", tool="rdp-session.exe")

        self.assertTrue(report.detached)
        command = run.call_args.args[0]
        self.assertIn("--password-env", command)
        self.assertIn("RDP_PASSWORD", command)
        self.assertNotIn("--password-stdin", command)
        self.assertIsNone(run.call_args.kwargs["input"])

    @patch("rdp_session.client.subprocess.run")
    def test_sends_direct_password_through_stdin_as_utf8(self, run):
        report = dict(REPORT)
        report["username"] = "ä用户"
        run.return_value = completed(
            stdout=json.dumps(report, ensure_ascii=False).encode("utf-8")
        )

        result = create_session(
            username="appuser",
            password="päss🔒",
            tool="rdp-session.exe",
        )

        command = run.call_args.args[0]
        self.assertEqual(result.username, "ä用户")
        self.assertIn("--password-stdin", command)
        self.assertNotIn("--password-env", command)
        self.assertNotIn("päss🔒", command)
        self.assertEqual(run.call_args.kwargs["input"], "päss🔒".encode("utf-8"))

    @patch("rdp_session.client.subprocess.run")
    def test_raises_structured_cli_error(self, run):
        run.return_value = completed(
            stdout=b"",
            stderr=b'{"ok":false,"kind":"config","exit_code":10,"error":"bad input"}\n',
            returncode=10,
        )

        with self.assertRaises(RdpSessionError) as raised:
            create_session(username="appuser", tool="rdp-session.exe")

        self.assertEqual(raised.exception.kind, "config")
        self.assertEqual(raised.exception.exit_code, 10)
        self.assertEqual(str(raised.exception), "bad input")

    @patch("rdp_session.client.subprocess.run")
    def test_passes_optional_create_arguments(self, run):
        run.return_value = completed()

        create_session(
            username="appuser",
            domain=".",
            allow_insecure_cert=True,
            screenshot=r"C:\Temp\desktop.png",
            dismiss_action="enter",
            desktop_width=1440,
            desktop_height=900,
            tool="rdp-session.exe",
        )

        command = run.call_args.args[0]
        self.assertIn("--domain", command)
        self.assertIn(".", command)
        self.assertIn("--allow-insecure-cert", command)
        self.assertIn("--screenshot", command)
        self.assertIn(r"C:\Temp\desktop.png", command)
        self.assertIn("--desktop-width", command)
        self.assertIn("1440", command)

    @patch("rdp_session.client.subprocess.run")
    def test_env_values_are_merged_with_process_environment(self, run):
        run.return_value = completed()

        create_session(
            username="appuser",
            password_env="RDP_PASSWORD",
            env={"RDP_PASSWORD": "secret"},
            tool="rdp-session.exe",
        )

        child_env = run.call_args.kwargs["env"]
        self.assertEqual(child_env["RDP_PASSWORD"], "secret")
        self.assertEqual(child_env.get("PATH"), os.environ.get("PATH"))

    @patch("rdp_session.client.subprocess.run")
    def test_explicit_tool_wins_over_bundled_binary(self, run):
        run.return_value = completed()

        create_session(username="appuser", tool=r"C:\Tools\rdp-session.exe")

        self.assertEqual(run.call_args.args[0][0], r"C:\Tools\rdp-session.exe")

    @patch("rdp_session.client._package_root")
    @patch("rdp_session.client.platform.machine")
    @patch("rdp_session.client.platform.system")
    @patch("rdp_session.client.shutil.which")
    @patch("rdp_session.client.subprocess.run")
    def test_uses_bundled_windows_binary_before_path(
        self,
        run,
        which,
        system,
        machine,
        package_root,
    ):
        binary = b"bundled"
        run.return_value = completed()
        which.return_value = r"C:\Old\rdp-session.exe"
        system.return_value = "Windows"
        machine.return_value = "ARM64"

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package_root.return_value = root
            bundled = root / "bin" / "rdp-session-windows-arm64.exe"
            bundled.parent.mkdir()
            bundled.write_bytes(binary)
            bundled.with_name(f"{bundled.name}.sha256").write_text(
                sha256_text(binary, bundled.name).decode("ascii"),
                encoding="ascii",
            )

            create_session(username="appuser")

            self.assertEqual(run.call_args.args[0][0], str(bundled))
            which.assert_not_called()

    @patch("rdp_session.client._package_root")
    @patch("rdp_session.client.platform.machine")
    @patch("rdp_session.client.platform.system")
    @patch("rdp_session.client.shutil.which")
    @patch("rdp_session.client.subprocess.run")
    def test_rejects_bundled_binary_checksum_mismatch(
        self,
        run,
        which,
        system,
        machine,
        package_root,
    ):
        system.return_value = "Windows"
        machine.return_value = "AMD64"

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package_root.return_value = root
            bundled = root / "bin" / "rdp-session-windows-x86_64.exe"
            bundled.parent.mkdir()
            bundled.write_bytes(b"actual")
            bundled.with_name(f"{bundled.name}.sha256").write_text(
                sha256_text(b"expected", bundled.name).decode("ascii"),
                encoding="ascii",
            )

            with self.assertRaisesRegex(RdpSessionError, "checksum mismatch"):
                create_session(username="appuser")

            run.assert_not_called()
            which.assert_not_called()

    @patch("rdp_session.client.urllib.request.urlopen")
    @patch("rdp_session.client.importlib.metadata.version")
    @patch("rdp_session.client.platform.machine")
    @patch("rdp_session.client.platform.system")
    @patch("rdp_session.client.shutil.which")
    @patch("rdp_session.client.subprocess.run")
    def test_downloads_missing_windows_binary(
        self,
        run,
        which,
        system,
        machine,
        version,
        urlopen,
    ):
        binary = b"binary"
        run.return_value = completed()
        which.return_value = None
        system.return_value = "Windows"
        machine.return_value = "AMD64"
        version.return_value = "0.2.1"
        urlopen.side_effect = [
            response(sha256_text(binary, "rdp-session-v0.2.1-windows-x86_64.exe")),
            response(binary),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            create_session(
                username="appuser",
                env={"LOCALAPPDATA": temp_dir},
            )

            expected = (
                Path(temp_dir)
                / "rdp-session"
                / "bin"
                / "v0.2.1"
                / "rdp-session-v0.2.1-windows-x86_64.exe"
            )
            self.assertEqual(run.call_args.args[0][0], str(expected))
            self.assertEqual(expected.read_bytes(), binary)
            self.assertEqual(
                urlopen.call_args_list,
                [
                    call(
                        "https://github.com/jqwn/rdp-session/releases/download/"
                        "v0.2.1/rdp-session-v0.2.1-windows-x86_64.exe.sha256",
                        timeout=60,
                    ),
                    call(
                        "https://github.com/jqwn/rdp-session/releases/download/"
                        "v0.2.1/rdp-session-v0.2.1-windows-x86_64.exe",
                        timeout=60,
                    ),
                ],
            )

    @patch("rdp_session.client.urllib.request.urlopen")
    @patch("rdp_session.client.importlib.metadata.version")
    @patch("rdp_session.client.platform.machine")
    @patch("rdp_session.client.platform.system")
    @patch("rdp_session.client.shutil.which")
    @patch("rdp_session.client.subprocess.run")
    def test_redownloads_corrupt_cached_windows_binary(
        self,
        run,
        which,
        system,
        machine,
        version,
        urlopen,
    ):
        binary = b"binary"
        run.return_value = completed()
        which.return_value = None
        system.return_value = "Windows"
        machine.return_value = "ARM64"
        version.return_value = "0.2.1"
        urlopen.side_effect = [
            response(sha256_text(binary, "rdp-session-v0.2.1-windows-arm64.exe")),
            response(binary),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            cached = (
                Path(temp_dir)
                / "rdp-session"
                / "bin"
                / "v0.2.1"
                / "rdp-session-v0.2.1-windows-arm64.exe"
            )
            cached.parent.mkdir(parents=True)
            cached.write_bytes(b"corrupt")

            create_session(username="appuser", env={"LOCALAPPDATA": temp_dir})

            self.assertEqual(run.call_args.args[0][0], str(cached))
            self.assertEqual(cached.read_bytes(), binary)

    @patch("rdp_session.client.urllib.request.urlopen")
    @patch("rdp_session.client.importlib.metadata.version")
    @patch("rdp_session.client.platform.machine")
    @patch("rdp_session.client.platform.system")
    @patch("rdp_session.client.shutil.which")
    @patch("rdp_session.client.subprocess.run")
    def test_rejects_truncated_download(
        self,
        run,
        which,
        system,
        machine,
        version,
        urlopen,
    ):
        run.return_value = completed()
        which.return_value = None
        system.return_value = "Windows"
        machine.return_value = "AMD64"
        version.return_value = "0.2.1"
        urlopen.side_effect = [
            response(sha256_text(b"complete", "rdp-session-v0.2.1-windows-x86_64.exe")),
            response(b"truncated"),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(RdpSessionError):
                create_session(username="appuser", env={"LOCALAPPDATA": temp_dir})

            cache_dir = Path(temp_dir) / "rdp-session" / "bin" / "v0.2.1"
            self.assertFalse(
                (cache_dir / "rdp-session-v0.2.1-windows-x86_64.exe").exists()
            )

    @patch("rdp_session.client.urllib.request.urlopen")
    @patch("rdp_session.client.importlib.metadata.version")
    @patch("rdp_session.client.platform.machine")
    @patch("rdp_session.client.platform.system")
    @patch("rdp_session.client.shutil.which")
    @patch("rdp_session.client.subprocess.run")
    def test_uses_verified_cached_windows_binary_without_binary_download(
        self,
        run,
        which,
        system,
        machine,
        version,
        urlopen,
    ):
        cached_bytes = b"cached"
        run.return_value = completed()
        which.return_value = None
        system.return_value = "Windows"
        machine.return_value = "ARM64"
        version.return_value = "0.2.1"
        urlopen.return_value = response(
            sha256_text(cached_bytes, "rdp-session-v0.2.1-windows-arm64.exe")
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            cached = (
                Path(temp_dir)
                / "rdp-session"
                / "bin"
                / "v0.2.1"
                / "rdp-session-v0.2.1-windows-arm64.exe"
            )
            cached.parent.mkdir(parents=True)
            cached.write_bytes(cached_bytes)

            create_session(username="appuser", env={"LOCALAPPDATA": temp_dir})

            self.assertEqual(run.call_args.args[0][0], str(cached))
            urlopen.assert_called_once()

    @patch("rdp_session.client.urllib.request.urlopen")
    @patch("rdp_session.client.importlib.metadata.version")
    @patch("rdp_session.client.platform.machine")
    @patch("rdp_session.client.platform.system")
    @patch("rdp_session.client.shutil.which")
    def test_rejects_non_regular_cached_windows_binary(
        self,
        which,
        system,
        machine,
        version,
        urlopen,
    ):
        which.return_value = None
        system.return_value = "Windows"
        machine.return_value = "ARM64"
        version.return_value = "0.2.1"
        urlopen.return_value = response(
            sha256_text(b"cached", "rdp-session-v0.2.1-windows-arm64.exe")
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            cached = (
                Path(temp_dir)
                / "rdp-session"
                / "bin"
                / "v0.2.1"
                / "rdp-session-v0.2.1-windows-arm64.exe"
            )
            cached.mkdir(parents=True)

            with self.assertRaisesRegex(RdpSessionError, "not a regular file"):
                create_session(username="appuser", env={"LOCALAPPDATA": temp_dir})

    @patch("rdp_session.client.urllib.request.urlopen")
    @patch("rdp_session.client.importlib.metadata.version")
    @patch("rdp_session.client.platform.machine")
    @patch("rdp_session.client.platform.system")
    @patch("rdp_session.client.shutil.which")
    @patch("rdp_session.client.subprocess.run")
    def test_concurrent_first_use_uses_unique_temporary_files(
        self,
        run,
        which,
        system,
        machine,
        version,
        urlopen,
    ):
        binary = b"binary"
        barrier = threading.Barrier(2)
        run.return_value = completed()
        which.return_value = None
        system.return_value = "Windows"
        machine.return_value = "AMD64"
        version.return_value = "0.2.1"

        def open_url(url, timeout):
            self.assertEqual(timeout, 60)
            if url.endswith(".sha256"):
                return response(
                    sha256_text(binary, "rdp-session-v0.2.1-windows-x86_64.exe")
                )
            barrier.wait(timeout=5)
            return response(binary)

        urlopen.side_effect = open_url

        with tempfile.TemporaryDirectory() as temp_dir:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        create_session,
                        username="appuser",
                        env={"LOCALAPPDATA": temp_dir},
                    )
                    for _ in range(2)
                ]
                for future in futures:
                    self.assertTrue(future.result().detached)

            cached = (
                Path(temp_dir)
                / "rdp-session"
                / "bin"
                / "v0.2.1"
                / "rdp-session-v0.2.1-windows-x86_64.exe"
            )
            self.assertEqual(cached.read_bytes(), binary)

    @patch("rdp_session.client.subprocess.run")
    def test_rejects_invalid_response_types(self, run):
        report = dict(REPORT)
        report["detached"] = "false"
        run.return_value = completed(stdout=json.dumps(report).encode("utf-8"))

        with self.assertRaisesRegex(RdpSessionError, "invalid rdp-session response"):
            create_session(username="appuser", tool="rdp-session.exe")

    @patch("rdp_session.client.subprocess.run")
    def test_wraps_process_creation_os_error(self, run):
        run.side_effect = PermissionError("access denied")

        with self.assertRaisesRegex(RdpSessionError, "could not start"):
            create_session(username="appuser", tool="rdp-session.exe")

    @patch("rdp_session.client.platform.machine")
    @patch("rdp_session.client.platform.system")
    @patch("rdp_session.client.shutil.which")
    @patch("rdp_session.client.importlib.metadata.version")
    def test_rejects_non_release_versions_for_automatic_download(
        self,
        version,
        which,
        system,
        machine,
    ):
        version.return_value = "0.2.1.dev1"
        which.return_value = None
        system.return_value = "Windows"
        machine.return_value = "AMD64"

        with self.assertRaisesRegex(RdpSessionError, "stable version"):
            create_session(username="appuser")


if __name__ == "__main__":
    unittest.main()
