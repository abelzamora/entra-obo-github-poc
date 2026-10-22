#!/usr/bin/env python3
"""Create and install the POC GitHub App using GitHub's local manifest flow."""

from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from envfile import ROOT, read_env, write_env

GITHUB_CALLBACK = "http://localhost:8002/github/callback"
MANIFEST_API_VERSION = "2026-03-10"


class ManifestServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: str, manifest: dict):
        super().__init__(address, ManifestHandler)
        self.expected_state = state
        self.manifest = manifest
        self.callback_event = threading.Event()
        self.code: str | None = None
        self.error: str | None = None


class ManifestHandler(BaseHTTPRequestHandler):
    server: ManifestServer

    def log_message(self, _format: str, *_args: object) -> None:
        # Callback URLs contain a one-time code; never log request paths.
        return

    def send_html(self, content: str, status: int = 200) -> None:
        payload = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/":
            manifest = html.escape(json.dumps(self.server.manifest, separators=(",", ":")), quote=True)
            state = urllib.parse.quote(self.server.expected_state, safe="")
            self.send_html(
                "<!doctype html><meta charset=utf-8><title>Register local POC GitHub App</title>"
                "<h1>Register the local POC GitHub App</h1>"
                "<p>This opens GitHub. Review the prefilled settings, then click <b>Create GitHub App</b>.</p>"
                f'<form method="post" action="https://github.com/settings/apps/new?state={state}">'
                f'<input type="hidden" name="manifest" value="{manifest}">'
                '<button type="submit">Continue to GitHub</button></form>'
            )
            return
        if parsed.path != "/callback":
            self.send_html("Not found", 404)
            return
        query = urllib.parse.parse_qs(parsed.query)
        received_state = query.get("state", [""])[0]
        code = query.get("code", [""])[0]
        if not secrets.compare_digest(received_state, self.server.expected_state):
            self.server.error = "State validation failed. Close this window and rerun the script."
            self.server.callback_event.set()
            self.send_html("<h1>Setup callback rejected</h1><p>Return to the terminal.</p>", 400)
            return
        if not code:
            self.server.error = "GitHub did not return a setup code. Close this window and rerun the script."
            self.server.callback_event.set()
            self.send_html("<h1>GitHub did not return a code</h1><p>Return to the terminal.</p>", 400)
            return
        self.server.code = code
        self.server.callback_event.set()
        self.send_html("<h1>GitHub App registered</h1><p>Return to the setup terminal to finish saving its local configuration.</p>")


def convert_manifest_code(code: str) -> dict:
    request = urllib.request.Request(
        f"https://api.github.com/app-manifests/{urllib.parse.quote(code, safe='')}/conversions",
        data=b"",
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": MANIFEST_API_VERSION,
            "User-Agent": "agentic-products-factory-obo-poc-setup",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError("Could not complete the GitHub App manifest exchange. The one-time code may have expired; rerun the script.") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="Local callback port; default chooses an available port.")
    parser.add_argument("--force-new", action="store_true", help="Create another GitHub App even if credentials are already configured in .env.")
    args = parser.parse_args()

    existing = read_env()
    if existing.get("GITHUB_APP_CLIENT_ID") and existing.get("GITHUB_APP_CLIENT_SECRET") and not args.force_new:
        print("GitHub App credentials are already configured in .env; skipping creation to avoid a duplicate app.")
        print("If you intend to register another GitHub App, rerun with --force-new.")
        return 0

    state = secrets.token_urlsafe(32)
    manifest = {
        "name": "agentic-products-factory-poc",
        "description": "Local Entra OBO and GitHub repository listing proof of concept",
        "url": "http://localhost:8001",
        "redirect_url": "http://127.0.0.1:{port}/callback",
        "callback_urls": [GITHUB_CALLBACK],
        "public": False,
        "request_oauth_on_install": False,
        "default_permissions": {"metadata": "read"},
    }
    server: ManifestServer | None = None
    try:
        server = ManifestServer(("127.0.0.1", args.port), state, manifest)
        manifest["redirect_url"] = f"http://127.0.0.1:{server.server_port}/callback"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        local_url = f"http://127.0.0.1:{server.server_port}/"
        print("Opening GitHub's app creation page in your browser. Sign in there if prompted and create the app.")
        print("The setup callback is local-only; the one-time code and returned credentials will not be printed.")
        if not webbrowser.open(local_url):
            print(f"Open this local URL in your browser: {local_url}")
        if not server.callback_event.wait(timeout=600):
            raise RuntimeError("Timed out waiting for GitHub. Rerun the script to start a fresh registration.")
        if server.error:
            raise RuntimeError(server.error)
        if not server.code:
            raise RuntimeError("GitHub callback did not contain a registration code.")
        server.shutdown()
        thread.join(timeout=5)
        created = convert_manifest_code(server.code)

        client_id = created.get("client_id")
        client_secret = created.get("client_secret")
        app_id = created.get("id")
        private_key = created.get("pem")
        if not all((client_id, client_secret, app_id, private_key)):
            raise RuntimeError("GitHub's response did not include all expected app credentials; no values were printed.")

        secrets_dir = ROOT / "secrets"
        secrets_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(secrets_dir, 0o700)
        key_path = secrets_dir / "github-app-private-key.pem"
        key_path.write_text(private_key, encoding="utf-8")
        os.chmod(key_path, 0o600)
        write_env({
            "GITHUB_APP_ID": str(app_id),
            "GITHUB_APP_CLIENT_ID": str(client_id),
            "GITHUB_APP_CLIENT_SECRET": str(client_secret),
            "GITHUB_CALLBACK_URL": GITHUB_CALLBACK,
            "GITHUB_APP_PRIVATE_KEY_PATH": "./secrets/github-app-private-key.pem",
        })
        print(f"Saved GitHub client credentials to {ROOT / '.env'} and the generated private key to {key_path}.")
        print("The .env file and private-key directory have owner-only permissions and are git-ignored.")

        slug = created.get("slug")
        if slug:
            install_url = f"https://github.com/apps/{urllib.parse.quote(slug, safe='-')}/installations/new"
            print("Opening the installation page. Select your personal account, choose Only select repositories, select the repositories to expose, then approve installation.")
            webbrowser.open(install_url)
        else:
            print("Install the app on your personal account and select All repositories.")
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"GitHub setup stopped: {exc}", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
