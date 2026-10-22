#!/usr/bin/env python3
"""Create/update the two Entra app registrations used by this local OBO POC."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from envfile import ROOT, read_env, write_env

TENANT_ID = "662f928d-ee5c-4b0a-a0f7-0072c9c0d1bd"
APP1_NAME = "agentic_products"
APP2_NAME = "factory"
APP1_REDIRECT = "http://localhost:8001/auth/callback"
APP2_REDIRECT = "http://localhost:8002/auth/callback"
SCOPE_NAME = "access_as_user"


class SetupError(RuntimeError):
    pass


def az_json(args: list[str]) -> object:
    command = ["az", *args, "--output", "json"]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "Azure CLI command failed"
        raise SetupError(f"{command[1]} command failed: {detail}")
    if not result.stdout.strip():
        # Microsoft Graph PATCH requests commonly return HTTP 204 with no body.
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SetupError("Azure CLI returned invalid JSON") from exc


def az_rest(method: str, url: str, body: dict | None = None) -> object:
    args = ["rest", "--method", method, "--url", url]
    if body is not None:
        args.extend(["--headers", "Content-Type=application/json", "--body", json.dumps(body)])
    return az_json(args)


def ensure_app(name: str, redirect_uri: str | None = None) -> dict:
    escaped_name = name.replace("'", "''")
    apps = az_json(["ad", "app", "list", "--filter", f"displayName eq '{escaped_name}'"])
    matches = [app for app in apps if app.get("displayName") == name]
    if len(matches) > 1:
        raise SetupError(f"Multiple registrations are named {name!r}; rename/remove duplicates and rerun.")
    if matches:
        app = matches[0]
        print(f"Using existing registration {name}.")
    else:
        args = ["ad", "app", "create", "--display-name", name, "--sign-in-audience", "AzureADMyOrg"]
        if redirect_uri:
            args.extend(["--web-redirect-uris", redirect_uri])
        app = az_json(args)
        print(f"Created registration {name}.")
    if redirect_uri:
        web = app.get("web") or {}
        redirects = list(web.get("redirectUris") or [])
        if redirect_uri not in redirects:
            redirects.append(redirect_uri)
        az_rest("PATCH", f"https://graph.microsoft.com/v1.0/applications/{app['id']}", {"web": {"redirectUris": redirects}})
    return app


def ensure_scope(app: dict, display_name: str) -> dict:
    app = az_json(["ad", "app", "show", "--id", app["appId"]])
    api = dict(app.get("api") or {})
    scopes = list(api.get("oauth2PermissionScopes") or [])
    scope = next((item for item in scopes if item.get("value") == SCOPE_NAME), None)
    if scope is None:
        scope = {
            "adminConsentDescription": f"Allow the signed-in user to access the {display_name} API.",
            "adminConsentDisplayName": f"Access {display_name}",
            "id": str(uuid.uuid4()),
            "isEnabled": True,
            "type": "User",
            "userConsentDescription": f"Allow this application to access the {display_name} API on your behalf.",
            "userConsentDisplayName": f"Access {display_name} on your behalf",
            "value": SCOPE_NAME,
        }
        scopes.append(scope)
    else:
        scope["isEnabled"] = True
    api["oauth2PermissionScopes"] = scopes
    api["requestedAccessTokenVersion"] = 2
    identifier_uri = f"api://{app['appId']}"
    identifier_uris = list(app.get("identifierUris") or [])
    if identifier_uri not in identifier_uris:
        identifier_uris.append(identifier_uri)
    az_rest(
        "PATCH",
        f"https://graph.microsoft.com/v1.0/applications/{app['id']}",
        {"api": api, "identifierUris": identifier_uris},
    )
    return scope


def ensure_service_principal(app_id: str) -> None:
    result = subprocess.run(
        ["az", "ad", "sp", "show", "--id", app_id, "--output", "none"],
        text=True,
        capture_output=True,
    )
    if result.returncode == 0:
        return
    az_json(["ad", "sp", "create", "--id", app_id])


def ensure_app1_permissions(app1: dict, app1_scope: dict, app2: dict, app2_scope: dict) -> None:
    app1 = az_json(["ad", "app", "show", "--id", app1["appId"]])
    existing = list(app1.get("requiredResourceAccess") or [])
    by_resource = {item["resourceAppId"]: item for item in existing if item.get("resourceAppId")}
    # App 1 requests its own scope for Token A and Factory's scope for the OBO exchange.
    for resource_app_id, scope in ((app1["appId"], app1_scope), (app2["appId"], app2_scope)):
        entry = by_resource.setdefault(resource_app_id, {"resourceAppId": resource_app_id, "resourceAccess": []})
        access = entry.setdefault("resourceAccess", [])
        if not any(item.get("id") == scope["id"] and item.get("type") == "Scope" for item in access):
            access.append({"id": scope["id"], "type": "Scope"})
    az_rest(
        "PATCH",
        f"https://graph.microsoft.com/v1.0/applications/{app1['id']}",
        {"requiredResourceAccess": list(by_resource.values())},
    )


def ensure_secret(app: dict, env_key: str, label: str, values: dict[str, str]) -> str:
    if values.get(env_key):
        print(f"Keeping the existing {label} secret from .env.")
        return values[env_key]
    result = az_json([
        "ad", "app", "credential", "reset", "--id", app["appId"], "--append",
        "--display-name", f"{label} local POC", "--years", "1",
    ])
    secret = result.get("password")
    if not secret:
        raise SetupError(f"Azure CLI did not return a new {label} secret.")
    print(f"Created a one-year {label} secret and will save it only to .env.")
    return secret


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--grant-admin-consent",
        action="store_true",
        help="Grant tenant-wide consent for the two requested delegated scopes (Azure CLI documents this command as requiring Global Administrator).",
    )
    args = parser.parse_args()

    try:
        account = az_json(["account", "show"])
        if account.get("tenantId", "").lower() != TENANT_ID:
            raise SetupError(
                f"Azure CLI is signed into tenant {account.get('tenantId')!r}; expected {TENANT_ID}. "
                "Run az login --tenant 662f928d-ee5c-4b0a-a0f7-0072c9c0d1bd first."
            )

        app1 = ensure_app(APP1_NAME, APP1_REDIRECT)
        app2 = ensure_app(APP2_NAME, APP2_REDIRECT)
        app1_scope = ensure_scope(app1, APP1_NAME)
        app2_scope = ensure_scope(app2, APP2_NAME)
        az_rest(
            "PATCH",
            f"https://graph.microsoft.com/v1.0/applications/{app1['id']}",
            {"isFallbackPublicClient": True},
        )
        ensure_app1_permissions(app1, app1_scope, app2, app2_scope)
        ensure_service_principal(app1["appId"])
        ensure_service_principal(app2["appId"])

        values = read_env()
        secret = ensure_secret(app1, "ENTRA_APP1_CLIENT_SECRET", APP1_NAME, values)
        app2_secret = ensure_secret(app2, "ENTRA_APP2_CLIENT_SECRET", APP2_NAME, values)
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:
            raise SetupError("Run `uv sync --python 3.13` before this script.") from exc
        encryption_key = values.get("TOKEN_ENCRYPTION_KEY") or Fernet.generate_key().decode("ascii")
        write_env({
            "ENTRA_TENANT_ID": TENANT_ID,
            "ENTRA_APP1_CLIENT_ID": app1["appId"],
            "ENTRA_APP1_CLIENT_SECRET": secret,
            "ENTRA_APP1_SCOPE": f"api://{app1['appId']}/{SCOPE_NAME}",
            "APP1_REDIRECT_URI": APP1_REDIRECT,
            "APP1_BASE_URL": "http://localhost:8001",
            "ENTRA_APP2_CLIENT_ID": app2["appId"],
            "ENTRA_APP2_SCOPE": f"api://{app2['appId']}/{SCOPE_NAME}",
            "APP2_BASE_URL": "http://localhost:8002",
            "APP2_OAUTH_REDIRECT_URI": APP2_REDIRECT,
            "ENTRA_APP2_CLIENT_SECRET": app2_secret,
            "TOKEN_ENCRYPTION_KEY": encryption_key,
        })
        print(f"Updated {ROOT / '.env'} with Entra IDs, scopes, and local settings; secret values were not printed.")
        print("Factory is configured as both an OAuth web client and the OBO API resource. The local Agentic Products and Factory SAML Enterprise applications, and any hosted Factory SAML connection, are configured manually; see docs/configuration.md.")
        print("Next: grant consent in Entra API permissions, or rerun with --grant-admin-consent if you are a Global Administrator.")
        if args.grant_admin_consent:
            print("Requesting tenant-wide consent for the configured delegated scopes...")
            az_json(["ad", "app", "permission", "admin-consent", "--id", app1["appId"]])
            print("Tenant-wide consent granted.")
        return 0
    except (SetupError, OSError) as exc:
        print(f"Setup stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
