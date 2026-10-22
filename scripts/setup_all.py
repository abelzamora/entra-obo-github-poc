#!/usr/bin/env python3
"""Run the Entra CLI setup followed by the interactive GitHub App manifest flow."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grant-admin-consent", action="store_true")
    args = parser.parse_args()
    entra = [sys.executable, str(SCRIPTS / "setup_entra.py")]
    if args.grant_admin_consent:
        entra.append("--grant-admin-consent")
    if subprocess.run(entra).returncode:
        return 1
    return subprocess.run([sys.executable, str(SCRIPTS / "setup_github.py")]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
