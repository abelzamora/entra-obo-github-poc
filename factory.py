from __future__ import annotations

import os
import secrets
import sqlite3
import time
from html import escape
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from xml.etree import ElementTree

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jwt import PyJWKClient
from msal import ConfidentialClientApplication
from pydantic import BaseModel

load_dotenv()

TENANT_ID = os.getenv("ENTRA_TENANT_ID", "")
APP2_CLIENT_ID = os.getenv("ENTRA_APP2_CLIENT_ID", "")
APP2_CLIENT_SECRET = os.getenv("ENTRA_APP2_CLIENT_SECRET", "")
APP2_SCOPE_NAME = "access_as_user"
APP2_BASE_URL = os.getenv("APP2_BASE_URL", "http://localhost:8002").rstrip("/")
APP2_OAUTH_REDIRECT_URI = os.getenv("APP2_OAUTH_REDIRECT_URI", f"{APP2_BASE_URL}/auth/callback")
APP1_BASE_URL = os.getenv("APP1_BASE_URL", "http://localhost:8001").rstrip("/")
GITHUB_CLIENT_ID = os.getenv("GITHUB_APP_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_APP_CLIENT_SECRET", "")
GITHUB_APP_ID = os.getenv("GITHUB_APP_ID", "")
GITHUB_CALLBACK_URL = os.getenv("GITHUB_CALLBACK_URL", "http://localhost:8002/github/callback")
TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY", "")
TOKEN_DATABASE_PATH = os.getenv("TOKEN_DATABASE_PATH", "./local_tokens.sqlite3")
GITHUB_API_VERSION = "2026-03-10"
SAML_SP_ENTITY_ID = os.getenv("SAML_SP_ENTITY_ID", "http://localhost:8002/saml/metadata")
SAML_ACS_URL = os.getenv("SAML_ACS_URL", "http://localhost:8002/saml/acs")
SAML_IDP_METADATA_URL = os.getenv("SAML_IDP_METADATA_URL", "")
SAML_SESSION_COOKIE = "factory_saml_sid"
OAUTH_STATE_COOKIE = "factory_oauth_state"

ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
JWKS = PyJWKClient(f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys")
OAUTH_STATES: dict[str, dict[str, Any]] = {}
OAUTH_LOGIN_PENDING: dict[str, dict[str, Any]] = {}
OAUTH_SESSIONS: dict[str, dict[str, Any]] = {}
OAUTH_USERS: dict[tuple[str, str], dict[str, Any]] = {}
SAML_PENDING: dict[str, dict[str, Any]] = {}
SAML_SESSIONS: dict[str, dict[str, Any]] = {}
SAML_USERS: dict[tuple[str, str], dict[str, Any]] = {}

app = FastAPI(title="factory", docs_url="/docs")


def oauth_client() -> ConfidentialClientApplication:
    if not APP2_CLIENT_ID or not APP2_CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="Configure ENTRA_APP2_CLIENT_ID and ENTRA_APP2_CLIENT_SECRET in .env to use Factory OAuth sign-in.")
    return ConfidentialClientApplication(
        APP2_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        client_credential=APP2_CLIENT_SECRET,
    )


def validate_app1_return_to(return_to: str | None, default: str | None = None) -> str:
    candidate = return_to or default or APP2_BASE_URL
    target = urlparse(candidate)
    allowed = urlparse(APP1_BASE_URL)
    if target.scheme != "http" or target.netloc != allowed.netloc or target.path not in ("", "/", "/auth/logout") or target.fragment:
        raise HTTPException(status_code=400, detail="return_to must use the configured local Agentic Products URL")
    return candidate


def clear_expired_oauth_sessions() -> None:
    now = time.time()
    for state, context in list(OAUTH_LOGIN_PENDING.items()):
        if context.get("expires_at", 0) < now:
            OAUTH_LOGIN_PENDING.pop(state, None)
    for session_id, session in list(OAUTH_SESSIONS.items()):
        if session.get("expires_at", 0) < now:
            OAUTH_SESSIONS.pop(session_id, None)
    for key, session in list(OAUTH_USERS.items()):
        if session.get("expires_at", 0) < now:
            OAUTH_USERS.pop(key, None)


class ConnectRequest(BaseModel):
    return_to: str


def saml_library():
    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
        from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser
        from onelogin.saml2.settings import OneLogin_Saml2_Settings
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="Install the SAML dependencies with `uv sync` before using the local SAML demo.") from exc
    return OneLogin_Saml2_Auth, OneLogin_Saml2_IdPMetadataParser, OneLogin_Saml2_Settings


def saml_settings():
    if not SAML_IDP_METADATA_URL:
        raise HTTPException(status_code=503, detail="Set SAML_IDP_METADATA_URL in .env to the Entra Federation Metadata URL for the local SAML enterprise app.")
    OneLogin_Saml2_Auth, metadata_parser, OneLogin_Saml2_Settings = saml_library()
    try:
        idp_settings = metadata_parser.parse_remote(SAML_IDP_METADATA_URL, timeout=8)
        settings = {
            "strict": True,
            "debug": False,
            "sp": {
                "entityId": SAML_SP_ENTITY_ID,
                "assertionConsumerService": {
                    "url": SAML_ACS_URL,
                    "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
                },
                "singleLogoutService": {"url": "", "binding": ""},
                "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
                "x509cert": "",
                "privateKey": "",
            },
            "idp": idp_settings["idp"],
            "security": {
                "authnRequestsSigned": False,
                "logoutRequestSigned": False,
                "logoutResponseSigned": False,
                # Entra commonly signs the Assertion while leaving the outer
                # Response unsigned. Require and validate that assertion signature.
                "wantAssertionsSigned": True,
                "wantMessagesSigned": False,
                "wantNameId": True,
                "wantNameIdEncrypted": False,
                "wantAssertionsEncrypted": False,
                "wantXMLValidation": True,
                "rejectUnsolicitedResponsesWithInResponseTo": True,
            },
        }
        return OneLogin_Saml2_Auth, OneLogin_Saml2_Settings(settings)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not load or parse the configured Entra SAML metadata. Check SAML_IDP_METADATA_URL.") from exc


def saml_request_data(request: Request, post_data: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "https": "on" if request.url.scheme == "https" else "off",
        "http_host": request.url.netloc,
        "server_port": str(request.url.port or (443 if request.url.scheme == "https" else 80)),
        "script_name": request.url.path,
        "get_data": dict(request.query_params),
        "post_data": post_data or {},
    }


def claim_value(attributes: dict[str, Any], *names: str) -> str | None:
    wanted = {name.lower() for name in names}
    for key, values in attributes.items():
        tail = str(key).rstrip("/").rsplit("/", 1)[-1].lower()
        if tail in wanted:
            value = values[0] if isinstance(values, list) and values else values
            if value is not None:
                return str(value)
    return None


def saml_audiences(auth: Any) -> list[str]:
    """Read Audience values from the already validated SAML response XML."""
    response_xml = auth.get_last_response_xml()
    root = ElementTree.fromstring(response_xml)
    namespace = {"saml": "urn:oasis:names:tc:SAML:2.0:assertion"}
    return [
        node.text.strip()
        for node in root.findall(".//saml:Assertion/saml:Conditions/saml:AudienceRestriction/saml:Audience", namespace)
        if node.text and node.text.strip()
    ]


def clear_expired_saml_state() -> None:
    now = time.time()
    for key, value in list(SAML_PENDING.items()):
        if value["expires_at"] < now:
            SAML_PENDING.pop(key, None)
    for key, value in list(SAML_SESSIONS.items()):
        if value["expires_at"] < now:
            SAML_SESSIONS.pop(key, None)
    for key, value in list(SAML_USERS.items()):
        if value["expires_at"] < now:
            SAML_USERS.pop(key, None)


def validate_app2_token(authorization: str | None) -> dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="App 2 bearer token required")
    token = authorization.split(" ", 1)[1].strip()
    try:
        signing_key = JWKS.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=APP2_CLIENT_ID,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "aud", "iss", "tid", "oid"]},
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid Entra OBO access token") from exc
    if claims.get("tid") != TENANT_ID or not claims.get("oid"):
        raise HTTPException(status_code=403, detail="Token is not from the configured tenant")
    if APP2_SCOPE_NAME not in claims.get("scp", "").split():
        raise HTTPException(status_code=403, detail="Token lacks the access_as_user delegated scope")
    return claims


def encryption() -> Fernet:
    if not TOKEN_ENCRYPTION_KEY:
        raise HTTPException(status_code=500, detail="Set TOKEN_ENCRYPTION_KEY in .env")
    try:
        return Fernet(TOKEN_ENCRYPTION_KEY.encode())
    except Exception as exc:
        raise HTTPException(status_code=500, detail="TOKEN_ENCRYPTION_KEY must be a Fernet key") from exc


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(TOKEN_DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("""CREATE TABLE IF NOT EXISTS github_links (
        tenant_id TEXT NOT NULL,
        entra_oid TEXT NOT NULL,
        github_login TEXT NOT NULL,
        access_token TEXT NOT NULL,
        refresh_token TEXT,
        access_expires_at REAL,
        refresh_expires_at REAL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (tenant_id, entra_oid)
    )""")
    connection.commit()
    return connection


@dataclass
class GitHubLink:
    github_login: str
    access_token: str
    refresh_token: str | None
    access_expires_at: float | None
    refresh_expires_at: float | None


def save_link(claims: dict[str, Any], login: str, token_data: dict[str, Any]) -> None:
    cipher = encryption()
    access = cipher.encrypt(token_data["access_token"].encode()).decode()
    refresh_value = token_data.get("refresh_token")
    refresh = cipher.encrypt(refresh_value.encode()).decode() if refresh_value else None
    expires_in = token_data.get("expires_in")
    refresh_expires_in = token_data.get("refresh_token_expires_in")
    with db() as conn:
        conn.execute(
            """INSERT INTO github_links
            (tenant_id, entra_oid, github_login, access_token, refresh_token, access_expires_at, refresh_expires_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, entra_oid) DO UPDATE SET
            github_login=excluded.github_login, access_token=excluded.access_token,
            refresh_token=excluded.refresh_token, access_expires_at=excluded.access_expires_at,
            refresh_expires_at=excluded.refresh_expires_at, updated_at=excluded.updated_at""",
            (
                claims["tid"], claims["oid"], login, access, refresh,
                time.time() + int(expires_in) if expires_in else None,
                time.time() + int(refresh_expires_in) if refresh_expires_in else None,
                time.time(),
            ),
        )


def read_link(tenant_id: str, entra_oid: str) -> GitHubLink | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM github_links WHERE tenant_id=? AND entra_oid=?",
            (tenant_id, entra_oid),
        ).fetchone()
    if not row:
        return None
    cipher = encryption()
    try:
        access = cipher.decrypt(row["access_token"].encode()).decode()
        refresh = cipher.decrypt(row["refresh_token"].encode()).decode() if row["refresh_token"] else None
    except InvalidToken as exc:
        raise HTTPException(status_code=500, detail="Cannot decrypt local GitHub token; check TOKEN_ENCRYPTION_KEY") from exc
    return GitHubLink(row["github_login"], access, refresh, row["access_expires_at"], row["refresh_expires_at"])


def update_tokens(claims: dict[str, Any], token_data: dict[str, Any], existing: GitHubLink) -> None:
    refresh_ttl = token_data.get("refresh_token_expires_in")
    if refresh_ttl is None and existing.refresh_expires_at:
        refresh_ttl = max(0, int(existing.refresh_expires_at - time.time()))
    access_ttl = token_data.get("expires_in")
    if access_ttl is None and existing.access_expires_at:
        access_ttl = max(0, int(existing.access_expires_at - time.time()))
    merged = {
        "access_token": token_data.get("access_token", existing.access_token),
        "refresh_token": token_data.get("refresh_token", existing.refresh_token),
        "expires_in": access_ttl,
        "refresh_token_expires_in": refresh_ttl,
    }
    save_link(claims, existing.github_login, merged)


async def get_github_token(claims: dict[str, Any]) -> tuple[GitHubLink, str]:
    link = read_link(claims["tid"], claims["oid"])
    if not link:
        raise HTTPException(status_code=409, detail="GitHub is not connected. Use the Connect GitHub button first.")
    if link.access_expires_at and link.access_expires_at > time.time() + 90:
        return link, link.access_token
    if not link.refresh_token:
        raise HTTPException(status_code=409, detail="GitHub token expired. Reconnect GitHub.")
    if link.refresh_expires_at and link.refresh_expires_at <= time.time() + 90:
        raise HTTPException(status_code=409, detail="GitHub refresh token expired. Reconnect GitHub.")
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Set the GitHub App client ID and secret in .env")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": link.refresh_token,
            },
        )
    if response.is_error:
        raise HTTPException(status_code=502, detail="GitHub token refresh failed")
    payload = response.json()
    if payload.get("error") or not payload.get("access_token"):
        raise HTTPException(status_code=409, detail="GitHub token refresh was rejected. Reconnect GitHub.")
    update_tokens(claims, payload, link)
    new_link = read_link(claims["tid"], claims["oid"])
    return new_link or link, payload["access_token"]


async def github_api_get(url: str, access_token: str, params: dict[str, Any] | None = None) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30) as client:
        return await client.get(
            url,
            params=params,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {access_token}",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
        )


async def github_paginated_items(
    url: str,
    access_token: str,
    result_key: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    while url:
        response = await github_api_get(url, access_token, params)
        if response.status_code == 401:
            raise HTTPException(status_code=409, detail="GitHub authorization is no longer valid. Reconnect GitHub.")
        if response.status_code == 403:
            raise HTTPException(status_code=403, detail="GitHub denied access to the app installation repositories.")
        if response.is_error:
            raise HTTPException(status_code=502, detail=f"GitHub repository request failed ({response.status_code})")
        payload = response.json()
        page_items = payload if isinstance(payload, list) else payload.get(result_key, [])
        if isinstance(page_items, list):
            items.extend(item for item in page_items if isinstance(item, dict))
        url = response.links.get("next", {}).get("url", "")
        params = None
    return items


@app.post("/github/connect/start")
async def github_connect_start(body: ConnectRequest, authorization: str | None = Header(default=None)):
    claims = validate_app2_token(authorization)
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Set the GitHub App client ID and secret in .env")
    target = urlparse(body.return_to)
    allowed = {urlparse(APP1_BASE_URL).netloc}
    if target.scheme != "http" or target.netloc not in allowed or target.path not in ("", "/"):
        raise HTTPException(status_code=400, detail="return_to must use the configured local App 1 origin and an allowed path")
    state = secrets.token_urlsafe(32)
    OAUTH_STATES[state] = {
        "tid": claims["tid"],
        "oid": claims["oid"],
        "return_to": APP1_BASE_URL,
        "expires_at": time.time() + 10 * 60,
    }
    url = "https://github.com/login/oauth/authorize?" + urlencode(
        {
            "client_id": GITHUB_CLIENT_ID,
            "redirect_uri": GITHUB_CALLBACK_URL,
            "state": state,
        }
    )
    return {"authorization_url": url}


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    clear_expired_oauth_sessions()
    session = OAUTH_SESSIONS.get(request.cookies.get("factory_oauth_sid", ""))
    if session:
        identity = session["identity"]
        signed_in = (
            f"<p>OAuth session signed in as <b>{escape(identity.get('email') or identity.get('name') or 'Entra user')}</b>.</p>"
            f"<p><code>aud={escape(str(identity['aud']))}</code> · <code>tid={escape(identity['tid'])}</code> · <code>oid={escape(identity['oid'])}</code></p>"
        )
    else:
        signed_in = "<p>No active Factory OAuth session.</p>"
    return f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Factory · identity and OBO API</title><style>body{{font:16px system-ui;max-width:850px;margin:4rem auto;padding:0 1rem;color:#152238}}.card{{border:1px solid #d9e0e9;border-radius:12px;padding:18px;margin:14px 0}}a{{display:inline-block;padding:10px 14px;margin:5px;background:#2857a4;color:white;text-decoration:none;border-radius:8px}}code{{overflow-wrap:anywhere}}</style>
    <body><h1>Factory · standalone service</h1><p>This service provides an OBO-protected API and a separate interactive OAuth sign-in.</p>
    <section class="card"><h2>Factory OAuth</h2>{signed_in}<p>Redirect URI: <code>{escape(APP2_OAUTH_REDIRECT_URI)}</code></p><a href="/auth/login">Sign in with Entra OAuth</a><a href="/auth/logout">Sign out of OAuth</a></section>
    <section class="card"><h2>Factory SAML</h2><p>Separate SAML flow with its own Enterprise application and ACS.</p><a href="/saml/login">Sign in with Entra SAML</a><a href="/saml/logout">Sign out of SAML</a></section>
    <section class="card"><h2>OBO API</h2><p>Agentic Products calls this API with a delegated token whose audience is Factory.</p><p>The Factory app registration exposes <code>api://{escape(APP2_CLIENT_ID)}/access_as_user</code>.</p></section></body></html>"""


@app.get("/auth/login")
async def oauth_login(return_to: str | None = None):
    clear_expired_oauth_sessions()
    return_to = validate_app1_return_to(return_to, default=APP1_BASE_URL)
    client = oauth_client()
    flow = client.initiate_auth_code_flow(
        scopes=["openid", "profile", "email"],
        redirect_uri=APP2_OAUTH_REDIRECT_URI,
        prompt="select_account",
    )
    if "auth_uri" not in flow:
        raise HTTPException(status_code=500, detail="Could not start Factory OAuth sign-in")
    OAUTH_LOGIN_PENDING[flow["state"]] = {"flow": flow, "return_to": return_to, "expires_at": time.time() + 600}
    response = RedirectResponse(flow["auth_uri"])
    response.set_cookie(OAUTH_STATE_COOKIE, flow["state"], httponly=True, secure=False, samesite="lax", max_age=600)
    return response


@app.get("/auth/callback")
async def oauth_callback(request: Request):
    clear_expired_oauth_sessions()
    state = request.query_params.get("state", "")
    if not state or request.cookies.get(OAUTH_STATE_COOKIE) != state:
        raise HTTPException(status_code=400, detail="Factory OAuth state does not match this browser; restart sign-in")
    pending = OAUTH_LOGIN_PENDING.pop(state, None)
    if not pending or pending["expires_at"] < time.time():
        raise HTTPException(status_code=400, detail="Factory OAuth state expired or is invalid; start sign-in again")
    try:
        result = oauth_client().acquire_token_by_auth_code_flow(pending["flow"], dict(request.query_params))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Factory OAuth state validation failed") from exc
    identity = result.get("id_token_claims") or {}
    tenant_id, object_id = identity.get("tid"), identity.get("oid")
    if result.get("error") or not tenant_id or tenant_id != TENANT_ID or not object_id:
        raise HTTPException(status_code=401, detail="Factory OAuth sign-in failed or returned an unexpected tenant/user")
    safe_identity = {
        "aud": identity.get("aud", APP2_CLIENT_ID),
        "tid": tenant_id,
        "oid": object_id,
        "name": identity.get("name"),
        "email": identity.get("preferred_username") or identity.get("email"),
        "expires_at": time.time() + 8 * 60 * 60,
    }
    session_id = secrets.token_urlsafe(32)
    OAUTH_SESSIONS[session_id] = {"identity": safe_identity, "expires_at": safe_identity["expires_at"]}
    OAUTH_USERS[(tenant_id, object_id)] = safe_identity
    response = RedirectResponse(pending["return_to"])
    response.delete_cookie(OAUTH_STATE_COOKIE)
    response.set_cookie("factory_oauth_sid", session_id, httponly=True, secure=False, samesite="lax", max_age=8 * 60 * 60)
    return response


@app.get("/auth/logout")
async def oauth_logout(request: Request, return_to: str | None = None):
    session_id = request.cookies.get("factory_oauth_sid", "")
    session = OAUTH_SESSIONS.pop(session_id, None)
    if session:
        identity = session["identity"]
        OAUTH_USERS.pop((identity["tid"], identity["oid"]), None)
    target = validate_app1_return_to(return_to) if return_to else f"{APP2_BASE_URL}/"
    response = RedirectResponse(target)
    response.delete_cookie("factory_oauth_sid")
    response.delete_cookie(OAUTH_STATE_COOKIE)
    return response


@app.get("/demo/logout")
async def demo_logout(return_to: str | None = None):
    target = validate_app1_return_to(return_to, default=APP1_BASE_URL)
    response = RedirectResponse(target)
    response.delete_cookie("factory_oauth_sid")
    response.delete_cookie(OAUTH_STATE_COOKIE)
    response.delete_cookie(SAML_SESSION_COOKIE)
    return response


@app.get("/identity/validate")
async def validate_identity(authorization: str | None = Header(default=None)):
    """Validate an App 2 OBO token and return only the demo identity claims."""
    claims = validate_app2_token(authorization)
    return {"validated": True, "claims": {key: claims.get(key) for key in ("aud", "tid", "oid")}}


@app.get("/saml/metadata")
async def saml_metadata():
    _, settings = saml_settings()
    metadata = settings.get_sp_metadata()
    errors = settings.validate_metadata(metadata)
    if errors:
        raise HTTPException(status_code=500, detail="Local SAML service-provider metadata is invalid: " + ", ".join(errors))
    return Response(content=metadata, media_type="application/samlmetadata+xml")


@app.get("/saml/login")
async def saml_login(request: Request, return_to: str | None = None):
    clear_expired_saml_state()
    return_to = return_to or APP1_BASE_URL
    target = urlparse(return_to)
    allowed = {urlparse(APP1_BASE_URL).netloc}
    if target.scheme != "http" or target.netloc not in allowed or target.path not in ("", "/"):
        raise HTTPException(status_code=400, detail="return_to must be the configured local App 1 URL")
    OneLogin_Saml2_Auth, settings = saml_settings()
    relay_state = secrets.token_urlsafe(32)
    auth = OneLogin_Saml2_Auth(saml_request_data(request), old_settings=settings)
    authorization_url = auth.login(return_to=relay_state)
    request_id = auth.get_last_request_id()
    if not request_id:
        raise HTTPException(status_code=500, detail="SAML toolkit did not create an authentication request")
    SAML_PENDING[relay_state] = {
        "request_id": request_id,
        "return_to": return_to.rstrip("/"),
        "expires_at": time.time() + 10 * 60,
    }
    return RedirectResponse(authorization_url)


@app.post("/saml/acs")
async def saml_acs(request: Request):
    clear_expired_saml_state()
    body = await request.body()
    if len(body) > 2_000_000:
        raise HTTPException(status_code=413, detail="SAML response is too large")
    try:
        form_data = {
            key: values[0]
            for key, values in parse_qs(body.decode("utf-8"), keep_blank_values=True).items()
        }
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid SAML form response") from exc
    if not form_data.get("SAMLResponse") or not form_data.get("RelayState"):
        raise HTTPException(status_code=400, detail="SAMLResponse and RelayState are required")
    pending = SAML_PENDING.pop(form_data["RelayState"], None)
    if not pending or pending["expires_at"] < time.time():
        raise HTTPException(status_code=400, detail="SAML login state expired or is invalid; start again from the demo")
    OneLogin_Saml2_Auth, settings = saml_settings()
    auth = OneLogin_Saml2_Auth(saml_request_data(request, form_data), old_settings=settings)
    auth.process_response(request_id=pending["request_id"])
    if auth.get_errors() or not auth.is_authenticated():
        # Return validation codes only. Never log or return the assertion itself.
        errors = ", ".join(auth.get_errors()) or "not_authenticated"
        reason = auth.get_last_error_reason() or "No additional reason was provided by python3-saml."
        raise HTTPException(
            status_code=401,
            detail=f"Entra SAML response validation failed (errors: {errors}; reason: {reason}). Check the ACS, Entity ID, metadata, and signature configuration.",
        )
    attributes = auth.get_attributes()
    tenant_id = claim_value(attributes, "tenantid", "tid")
    object_id = claim_value(attributes, "objectidentifier", "oid")
    audiences = saml_audiences(auth)
    if tenant_id != TENANT_ID or not object_id or SAML_SP_ENTITY_ID not in audiences:
        raise HTTPException(status_code=403, detail="SAML assertion is valid but has the wrong tenant, missing user object ID, or wrong audience")
    identity = {
        "aud": audiences,
        "tid": tenant_id,
        "oid": object_id,
        "name_id": auth.get_nameid(),
        "attributes": {
            "email": claim_value(attributes, "emailaddress", "email", "upn", "name"),
        },
        "expires_at": time.time() + 8 * 60 * 60,
    }
    session_id = secrets.token_urlsafe(32)
    SAML_SESSIONS[session_id] = identity
    SAML_USERS[(tenant_id, object_id)] = identity
    response = RedirectResponse(f"{pending['return_to']}/?saml=connected")
    response.set_cookie(
        SAML_SESSION_COOKIE,
        session_id,
        httponly=True,
        secure=False,  # Localhost uses HTTP; use HTTPS and Secure cookies outside this demo.
        samesite="lax",
        max_age=8 * 60 * 60,
    )
    return response


@app.get("/saml/logout")
async def saml_logout(request: Request, return_to: str | None = None):
    session_id = request.cookies.get(SAML_SESSION_COOKIE)
    session = SAML_SESSIONS.pop(session_id or "", None)
    if session:
        SAML_USERS.pop((session["tid"], session["oid"]), None)
    return_to = return_to or APP1_BASE_URL
    target = urlparse(return_to)
    if target.scheme != "http" or target.netloc != urlparse(APP1_BASE_URL).netloc or target.path not in ("", "/", "/auth/logout"):
        raise HTTPException(status_code=400, detail="return_to must use the configured local App 1 origin and an allowed path")
    response = RedirectResponse(return_to)
    response.delete_cookie(SAML_SESSION_COOKIE)
    return response


@app.post("/demo/reset")
async def reset_demo(authorization: str | None = Header(default=None)):
    claims = validate_app2_token(authorization)
    with db() as conn:
        cursor = conn.execute(
            "DELETE FROM github_links WHERE tenant_id=? AND entra_oid=?",
            (claims["tid"], claims["oid"]),
        )
        github_link_removed = cursor.rowcount > 0
    saml = SAML_USERS.pop((claims["tid"], claims["oid"]), None)
    oauth = OAUTH_USERS.pop((claims["tid"], claims["oid"]), None)
    oauth_sessions_removed = 0
    for session_id, session in list(OAUTH_SESSIONS.items()):
        identity = session.get("identity", {})
        if identity.get("tid") == claims["tid"] and identity.get("oid") == claims["oid"]:
            OAUTH_SESSIONS.pop(session_id, None)
            oauth_sessions_removed += 1
    saml_sessions_removed = 0
    for session_id, session in list(SAML_SESSIONS.items()):
        if session.get("tid") == claims["tid"] and session.get("oid") == claims["oid"]:
            SAML_SESSIONS.pop(session_id, None)
            saml_sessions_removed += 1
    states_removed = 0
    for state, context in list(OAUTH_STATES.items()):
        if context.get("tid") == claims["tid"] and context.get("oid") == claims["oid"]:
            OAUTH_STATES.pop(state, None)
            states_removed += 1
    return {
        "reset": True,
        "github_link_removed": github_link_removed,
        "saml_session_removed": bool(saml) or saml_sessions_removed > 0,
        "oauth_session_removed": bool(oauth) or oauth_sessions_removed > 0,
        "pending_github_states_removed": states_removed,
        "note": "GitHub's external authorization remains active until revoked in GitHub Settings.",
    }


@app.get("/saml/status")
async def saml_status(authorization: str | None = Header(default=None)):
    claims = validate_app2_token(authorization)
    clear_expired_saml_state()
    session = SAML_USERS.get((claims["tid"], claims["oid"]))
    if not session:
        return {"connected": False, "claims": None}
    return {
        "connected": True,
        "claims": {key: session.get(key) for key in ("aud", "tid", "oid")},
        "email": session.get("attributes", {}).get("email"),
    }


@app.get("/oauth/status")
async def oauth_status(authorization: str | None = Header(default=None)):
    claims = validate_app2_token(authorization)
    clear_expired_oauth_sessions()
    identity = OAUTH_USERS.get((claims["tid"], claims["oid"]))
    if not identity:
        return {"connected": False, "claims": None}
    return {
        "connected": True,
        "claims": {key: identity.get(key) for key in ("aud", "tid", "oid")},
        "email": identity.get("email"),
    }


@app.get("/github/callback")
async def github_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        return RedirectResponse(f"{APP1_BASE_URL}/?github=denied")
    context = OAUTH_STATES.pop(state or "", None)
    if not context or context["expires_at"] < time.time():
        raise HTTPException(status_code=400, detail="GitHub authorization state expired or is invalid")
    if not code:
        raise HTTPException(status_code=400, detail="GitHub did not return an authorization code")
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Set the GitHub App client ID and secret in .env")
    async with httpx.AsyncClient(timeout=20) as client:
        token_response = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": GITHUB_CALLBACK_URL,
            },
        )
        if token_response.is_error:
            raise HTTPException(status_code=502, detail="GitHub token exchange failed")
        token_data = token_response.json()
        if token_data.get("error") or not token_data.get("access_token"):
            raise HTTPException(status_code=400, detail="GitHub did not issue an access token")
        user_response = await client.get(
            "https://api.github.com/user",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token_data['access_token']}",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
        )
    if user_response.is_error:
        raise HTTPException(status_code=502, detail="Could not read the authorized GitHub account")
    github_user = user_response.json()
    save_link(context, github_user["login"], token_data)
    return RedirectResponse(f"{context['return_to']}/?github=connected")


@app.get("/github/status")
async def github_status(authorization: str | None = Header(default=None)):
    claims = validate_app2_token(authorization)
    link = read_link(claims["tid"], claims["oid"])
    return {"connected": bool(link), "github_login": link.github_login if link else None}


@app.get("/github/repos")
async def github_repositories(authorization: str | None = Header(default=None)):
    claims = validate_app2_token(authorization)
    link, access_token = await get_github_token(claims)
    if not GITHUB_APP_ID:
        raise HTTPException(status_code=500, detail="Set GITHUB_APP_ID in .env to restrict listing to this app's installation.")

    # /user/repos describes the user's GitHub visibility and can exceed the
    # repository selection granted to this app. Resolve this app's installations,
    # then enumerate only repos returned by each installation-scoped endpoint.
    installations = await github_paginated_items(
        "https://api.github.com/user/installations",
        access_token,
        "installations",
        {"per_page": 100},
    )
    app_installations = [item for item in installations if str(item.get("app_id", "")) == GITHUB_APP_ID]
    if not app_installations:
        raise HTTPException(
            status_code=409,
            detail="No installation for this GitHub App is available to the connected user. Install the app and authorize it again.",
        )

    repo_by_id: dict[int | str, dict[str, Any]] = {}
    for installation in app_installations:
        installation_id = installation.get("id")
        if not installation_id:
            continue
        installation_repos = await github_paginated_items(
            f"https://api.github.com/user/installations/{installation_id}/repositories",
            access_token,
            "repositories",
            {"per_page": 100},
        )
        for item in installation_repos:
            if item.get("id") is not None:
                repo_by_id[item["id"]] = item

    repos = [
        {
            "id": item.get("id"),
            "full_name": item.get("full_name"),
            "private": item.get("private"),
            "html_url": item.get("html_url"),
            "description": item.get("description"),
            "default_branch": item.get("default_branch"),
            "permissions": item.get("permissions"),
        }
        for item in repo_by_id.values()
    ]
    return {
        "github_login": link.github_login,
        "count": len(repos),
        "repositories": repos,
        "note": "Repositories are restricted to those granted to this GitHub App's installations and visible to the connected user.",
    }


@app.get("/health")
async def health():
    return {"status": "ok", "application": "factory"}
