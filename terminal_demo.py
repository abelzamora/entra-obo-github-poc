from __future__ import annotations

import argparse
import os
import sys
import webbrowser

import httpx
from dotenv import load_dotenv
from msal import PublicClientApplication

load_dotenv()

TENANT_ID = os.getenv("ENTRA_TENANT_ID", "")
APP1_CLIENT_ID = os.getenv("ENTRA_APP1_CLIENT_ID", "")
APP1_SCOPE = os.getenv("ENTRA_APP1_SCOPE", "")
APP1_BASE_URL = os.getenv("APP1_BASE_URL", "http://localhost:8001").rstrip("/")


def acquire_token() -> str:
    if not all((TENANT_ID, APP1_CLIENT_ID, APP1_SCOPE)):
        raise SystemExit("Set ENTRA_TENANT_ID, ENTRA_APP1_CLIENT_ID and ENTRA_APP1_SCOPE in .env")
    client = PublicClientApplication(
        APP1_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
    )
    flow = client.initiate_device_flow(scopes=[APP1_SCOPE])
    if "user_code" not in flow:
        raise SystemExit(f"Could not start device sign-in: {flow}")
    print(flow["message"])
    result = client.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise SystemExit(result.get("error_description", result.get("error", "Sign-in failed")))
    return result["access_token"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Call the App 1 API using Entra device-code sign-in")
    parser.add_argument("command", choices=("connect", "repos", "status"))
    parser.add_argument("--open-browser", action="store_true", help="Open the GitHub authorization page for connect")
    args = parser.parse_args()
    token = acquire_token()
    headers = {"Authorization": f"Bearer {token}"}
    if args.command == "connect":
        response = httpx.post(f"{APP1_BASE_URL}/api/github/connect", headers=headers, timeout=30)
        if response.is_error:
            raise SystemExit(response.text)
        authorization_url = response.json()["authorization_url"]
        print("Open this URL to connect GitHub:")
        print(authorization_url)
        if args.open_browser:
            webbrowser.open(authorization_url)
        return
    endpoint = "/api/github/repos" if args.command == "repos" else "/api/github/status"
    response = httpx.get(f"{APP1_BASE_URL}{endpoint}", headers=headers, timeout=60)
    if response.is_error:
        raise SystemExit(response.text)
    print(response.text)


if __name__ == "__main__":
    main()
