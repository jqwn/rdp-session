# rdp-session

Small Rust CLI for creating a detached Windows RDP session with IronRDP.

The intended deployment is to copy `rdp-session.exe` to the Windows automation
machine and invoke it locally through PowerShell Remoting, Task Scheduler, SQL
Server `xp_cmdshell`, or a service account. The binary only creates/proves an
RDP session and then drops the transport. Caller-side orchestration should decide
whether a session is needed by using Windows-native commands such as `query user`.

## Build

```powershell
cargo build --release
```

The Windows binary will be at:

```text
target\release\rdp-session.exe
```

For the ARM64 Windows VM from macOS:

```sh
cargo zigbuild --release --target aarch64-pc-windows-gnullvm
```

## Create A Session

Password from environment:

```powershell
$env:RDP_PASSWORD = 'secret'
rdp-session.exe --output json create --host 127.0.0.1 --username appuser
```

Password from stdin:

```powershell
$password | rdp-session.exe --output json create --host 127.0.0.1 --username appuser --password-stdin
```

For a Microsoft-account-backed local Windows user, use the local profile/user
name with an explicit local domain:

```powershell
$password | rdp-session.exe --output json create --host 127.0.0.1 --username appuser --domain . --password-stdin
```

Do not pass `MicrosoftAccount\email@example.com` directly. IronRDP's CredSSP
username parser rejects that hybrid username format.

To capture proof that the RDP client reached the desktop before detaching, pass
`--screenshot`:

```powershell
$password | rdp-session.exe --output json create --host 127.0.0.1 --username appuser --domain . --password-stdin --screenshot C:\Temp\rdp-desktop.png
```

The requested virtual desktop size is controlled by `--desktop-width` and
`--desktop-height`; defaults are 1280 by 1024. The screenshot is saved at the
negotiated RDP framebuffer size after the active stage idles.

## Blocker Dismissal

Some customer machines show a legal notice or consent screen before the desktop.
The CLI can send one simple input action after the first graphics update:

```powershell
rdp-session.exe --output json create --host 127.0.0.1 --username appuser --dismiss-action enter
rdp-session.exe --output json create --host 127.0.0.1 --username appuser --dismiss-action click:640,520
```

Valid values are `none`, `enter`, and `click:x,y`.

## Certificate Scope

IronRDP certificate validation is not implemented in this prototype. Loopback
hosts (`127.0.0.1`, `::1`, `localhost`) are allowed by default because the normal
deployment is to run the binary on the target Windows machine. Non-local hosts
must opt in explicitly:

```powershell
rdp-session.exe --output json create --host server.example.com --allow-insecure-cert --username appuser
```

## Exit Codes

- `0`: success
- `1`: unexpected internal error
- `10`: configuration or input error
- `20`: authentication failed
- `21`: DNS, TCP, or TLS reachability error
- `22`: operation timeout
- `23`: desktop proof or screenshot failure
- `24`: RDP protocol failure after network connection

When `--output json` is used, successful reports are written to stdout. Logs and
errors are written to stderr, so stdout remains machine-readable.

## PowerShell Remoting Shape

```powershell
Invoke-Command -ComputerName WINHOST -ScriptBlock {
    $env:RDP_PASSWORD = '<set securely in caller>'
    C:\Tools\rdp-session.exe --output json create --host 127.0.0.1 --username appuser
}
```

See `scripts/create_gui_session.ps1` and `scripts/create_gui_session.py` for thin
wrappers around the create flow. They read the password from the same env/stdin
sources as the Rust CLI; they do not accept a direct password argument.

## Python Package

A subprocess-based Python package lives in `python/`. It invokes the Rust CLI
with `--output json` and returns the parsed create-session report.

```sh
python -m pip install -e .
```

Install from GitHub:

```sh
python -m pip install "git+https://github.com/jqwn/rdp-session.git"
```

For dependency files, pin to a tag or commit:

```text
rdp-session @ git+https://github.com/jqwn/rdp-session.git@<tag-or-commit>
```

Automatic binary download is supported for released tags whose package version
matches the release tag, for example `0.2.1` and `v0.2.1`. If you install from a
branch or arbitrary commit, pass `tool=...` or set `RDP_SESSION_BIN` to a binary
built from the same commit.

Use the default password environment variable:

```python
from rdp_session import create_session

report = create_session(
    host="127.0.0.1",
    username="appuser",
    screenshot=r"C:\Temp\rdp-desktop.png",
)
```

Or pass the password to the child process through stdin:

```python
report = create_session(
    host="127.0.0.1",
    username="appuser",
    password="secret",
)
```

The wrapper locates the CLI from `tool=...`, `RDP_SESSION_BIN`, or `PATH`.
On Windows x86_64 and ARM64, if no local binary is found, it downloads the
matching versioned release asset from GitHub and caches it under the user's
local app data directory. Cached and newly downloaded binaries are verified
against the release `.sha256` sidecar before execution. This catches corruption
and cache replacement, but it is not a substitute for code signing.

## Notes

- `status` and `ensure` are intentionally not part of the Rust CLI. Keeping that
  policy outside the binary avoids tying it to localhost-only Windows session
  inspection.
- Windows Pro supports RDP hosting for this use case, but should be treated as
  one interactive automation user per machine.
- For Task Scheduler or service-account use, run the binary on the target
  Windows machine and use `--host 127.0.0.1`.
