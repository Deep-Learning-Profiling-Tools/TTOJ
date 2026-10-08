#!/usr/bin/env python3
"""Create a private local configuration without overwriting existing secrets."""

import os
from pathlib import Path
import secrets
import sys


def main():
    root = Path(__file__).resolve().parent.parent
    destination = root / ".env"
    content = (
        "# Local credentials; do not commit this file.\n"
        "ADMIN_USERNAME=admin\n"
        f"ADMIN_PASSWORD={secrets.token_urlsafe(32)}\n"
        "JUDGE_USERNAME=judge\n"
        f"JUDGE_PASSWORD={secrets.token_urlsafe(32)}\n"
        "SERVICE_CPUSET=0-3,8-11\n"
        "BENCHMARK_CPUSET=4-7,12-15\n"
    )
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(".env already exists; it was left unchanged.", file=sys.stderr)
        return 1
    with os.fdopen(fd, "w") as output:
        output.write(content)
    print("Created .env with fresh credentials and mode 0600.")
    print("Review the CPU sets in .env before starting the services.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
