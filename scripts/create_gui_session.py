import argparse
import json
import os
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", default=r"C:\Tools\rdp-session.exe")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--username", required=True)
    parser.add_argument("--domain")
    parser.add_argument("--password-env", default="RDP_PASSWORD")
    parser.add_argument("--password-stdin", action="store_true")
    parser.add_argument("--allow-insecure-cert", action="store_true")
    parser.add_argument("--screenshot")
    parser.add_argument("--dismiss-action", default="none")
    args = parser.parse_args()

    if not args.password_stdin and not os.environ.get(args.password_env):
        raise SystemExit(f"{args.password_env} is not set")

    cmd = [
        args.tool,
        "--output",
        "json",
        "create",
        "--host",
        args.host,
        "--username",
        args.username,
        "--dismiss-action",
        args.dismiss_action,
    ]

    stdin_text = None
    if args.password_stdin:
        cmd.append("--password-stdin")
        stdin_text = sys.stdin.read()
    else:
        cmd.extend(["--password-env", args.password_env])

    if args.allow_insecure_cert:
        cmd.append("--allow-insecure-cert")

    if args.domain:
        cmd.extend(["--domain", args.domain])

    if args.screenshot:
        cmd.extend(["--screenshot", args.screenshot])

    completed = subprocess.run(
        cmd,
        text=True,
        input=stdin_text,
        capture_output=True,
    )

    if completed.stdout:
        try:
            print(json.dumps(json.loads(completed.stdout), indent=2))
        except json.JSONDecodeError:
            print(completed.stdout, end="")

    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")

    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
