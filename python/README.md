# rdp-session Python Wrapper

Thin Python wrapper around the `rdp-session` Rust CLI.

The package invokes the CLI through `subprocess.run`, requests JSON output, and
returns the parsed create-session report. It does not embed IronRDP directly.

## Install

```sh
python -m pip install -e .
```

From GitHub:

```sh
python -m pip install "git+https://github.com/jqwn/rdp-session.git"
```

As a pinned dependency:

```text
rdp-session @ git+https://github.com/jqwn/rdp-session.git@<tag-or-commit>
```

The Rust CLI is resolved in this order:

- pass `tool=...` to `create_session`
- set `RDP_SESSION_BIN`
- put `rdp-session.exe` on `PATH`
- on Windows x86_64 or ARM64, automatically download the matching versioned
  release asset from GitHub and cache it under the user's local app data
  directory

## Use

Password from environment:

```python
from rdp_session import create_session

report = create_session(
    host="127.0.0.1",
    username="appuser",
    password_env="RDP_PASSWORD",
    screenshot=r"C:\Temp\rdp-desktop.png",
)

print(report.detached)
```

Password through subprocess stdin:

```python
from rdp_session import create_session

report = create_session(
    host="127.0.0.1",
    username="appuser",
    password="secret",
)
```

Do not pass passwords in command-line arguments. When `password` is provided,
the wrapper sends it to the child process through stdin.
