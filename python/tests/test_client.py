import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


class CreateSessionTests(unittest.TestCase):
    @patch("rdp_session.client.subprocess.run")
    def test_uses_password_env_by_default(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(REPORT),
            stderr="",
        )

        report = create_session(username="appuser", tool="rdp-session.exe")

        self.assertTrue(report.detached)
        command = run.call_args.args[0]
        self.assertIn("--password-env", command)
        self.assertIn("RDP_PASSWORD", command)
        self.assertNotIn("--password-stdin", command)
        self.assertIsNone(run.call_args.kwargs["input"])

    @patch("rdp_session.client.subprocess.run")
    def test_sends_direct_password_through_stdin(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(REPORT),
            stderr="",
        )

        create_session(username="appuser", password="secret", tool="rdp-session.exe")

        command = run.call_args.args[0]
        self.assertIn("--password-stdin", command)
        self.assertNotIn("--password-env", command)
        self.assertEqual(run.call_args.kwargs["input"], "secret")

    @patch("rdp_session.client.subprocess.run")
    def test_raises_structured_cli_error(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=10,
            stdout="",
            stderr='{"ok":false,"kind":"config","exit_code":10,"error":"bad input"}\n',
        )

        with self.assertRaises(RdpSessionError) as raised:
            create_session(username="appuser", tool="rdp-session.exe")

        self.assertEqual(raised.exception.kind, "config")
        self.assertEqual(raised.exception.exit_code, 10)
        self.assertEqual(str(raised.exception), "bad input")

    @patch("rdp_session.client.subprocess.run")
    def test_passes_optional_create_arguments(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(REPORT),
            stderr="",
        )

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
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(REPORT),
            stderr="",
        )

        create_session(
            username="appuser",
            password_env="RDP_PASSWORD",
            env={"RDP_PASSWORD": "secret"},
            tool="rdp-session.exe",
        )

        child_env = run.call_args.kwargs["env"]
        self.assertEqual(child_env["RDP_PASSWORD"], "secret")
        self.assertEqual(child_env.get("PATH"), os.environ.get("PATH"))

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
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(REPORT),
            stderr="",
        )
        which.return_value = None
        system.return_value = "Windows"
        machine.return_value = "AMD64"
        version.return_value = "0.2.0"
        response = io.BytesIO(b"binary")
        response.status = 200
        response.__enter__ = lambda: response
        response.__exit__ = lambda *args: None
        urlopen.return_value = response

        with tempfile.TemporaryDirectory() as temp_dir:
            create_session(
                username="appuser",
                env={"LOCALAPPDATA": temp_dir},
            )

            expected = (
                Path(temp_dir)
                / "rdp-session"
                / "bin"
                / "v0.2.0"
                / "rdp-session-v0.2.0-windows-x86_64.exe"
            )
            self.assertEqual(run.call_args.args[0][0], str(expected))
            self.assertEqual(expected.read_bytes(), b"binary")
            urlopen.assert_called_once_with(
                "https://github.com/jqwn/rdp-session/releases/download/"
                "v0.2.0/rdp-session-v0.2.0-windows-x86_64.exe",
                timeout=60,
            )

    @patch("rdp_session.client.urllib.request.urlopen")
    @patch("rdp_session.client.importlib.metadata.version")
    @patch("rdp_session.client.platform.machine")
    @patch("rdp_session.client.platform.system")
    @patch("rdp_session.client.shutil.which")
    @patch("rdp_session.client.subprocess.run")
    def test_uses_cached_windows_binary_without_download(
        self,
        run,
        which,
        system,
        machine,
        version,
        urlopen,
    ):
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(REPORT),
            stderr="",
        )
        which.return_value = None
        system.return_value = "Windows"
        machine.return_value = "ARM64"
        version.return_value = "0.2.0"

        with tempfile.TemporaryDirectory() as temp_dir:
            cached = (
                Path(temp_dir)
                / "rdp-session"
                / "bin"
                / "v0.2.0"
                / "rdp-session-v0.2.0-windows-arm64.exe"
            )
            cached.parent.mkdir(parents=True)
            cached.write_bytes(b"cached")

            create_session(username="appuser", env={"LOCALAPPDATA": temp_dir})

            self.assertEqual(run.call_args.args[0][0], str(cached))
            urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
