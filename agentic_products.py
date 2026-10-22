from __future__ import annotations

import os
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from xml.etree import ElementTree

import httpx
import jwt
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jwt import PyJWKClient
from msal import ConfidentialClientApplication

load_dotenv()

TENANT_ID = os.getenv("ENTRA_TENANT_ID", "")
APP1_CLIENT_ID = os.getenv("ENTRA_APP1_CLIENT_ID", "")
APP1_CLIENT_SECRET = os.getenv("ENTRA_APP1_CLIENT_SECRET", "")
APP1_SCOPE = os.getenv("ENTRA_APP1_SCOPE", "")
APP1_REDIRECT_URI = os.getenv("APP1_REDIRECT_URI", "http://localhost:8001/auth/callback")
APP1_BASE_URL = os.getenv("APP1_BASE_URL", "http://localhost:8001").rstrip("/")
APP1_SAML_SP_ENTITY_ID = os.getenv("APP1_SAML_SP_ENTITY_ID", f"{APP1_BASE_URL}/saml/metadata")
APP1_SAML_ACS_URL = os.getenv("APP1_SAML_ACS_URL", f"{APP1_BASE_URL}/saml/acs")
APP1_SAML_IDP_METADATA_URL = os.getenv("APP1_SAML_IDP_METADATA_URL", "")
APP2_CLIENT_ID = os.getenv("ENTRA_APP2_CLIENT_ID", "")
APP2_SCOPE = os.getenv("ENTRA_APP2_SCOPE", "")
APP2_BASE_URL = os.getenv("APP2_BASE_URL", "http://localhost:8002").rstrip("/")

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
JWKS = PyJWKClient(f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys")
SESSIONS: dict[str, dict[str, Any]] = {}
SAML_PENDING: dict[str, dict[str, Any]] = {}

app = FastAPI(title="agentic_products", docs_url="/docs")


def saml_library():
    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
        from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser
        from onelogin.saml2.settings import OneLogin_Saml2_Settings
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="Install local SAML dependencies with `uv lock && uv sync --python 3.13`.") from exc
    return OneLogin_Saml2_Auth, OneLogin_Saml2_IdPMetadataParser, OneLogin_Saml2_Settings


def saml_settings():
    if not APP1_SAML_IDP_METADATA_URL:
        raise HTTPException(status_code=503, detail="Set APP1_SAML_IDP_METADATA_URL in .env to the Entra Federation Metadata URL for the local Agentic Products SAML enterprise app.")
    OneLogin_Saml2_Auth, metadata_parser, OneLogin_Saml2_Settings = saml_library()
    try:
        idp_settings = metadata_parser.parse_remote(APP1_SAML_IDP_METADATA_URL, timeout=8)
        settings = {
            "strict": True,
            "debug": False,
            "sp": {
                "entityId": APP1_SAML_SP_ENTITY_ID,
                "assertionConsumerService": {
                    "url": APP1_SAML_ACS_URL,
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
        raise HTTPException(status_code=503, detail="Could not load APP1_SAML_IDP_METADATA_URL. Check the local Agentic Products SAML app metadata URL.") from exc


def saml_request_data(request: Request, post_data: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "https": "on" if request.url.scheme == "https" else "off",
        "http_host": request.url.netloc,
        "server_port": str(request.url.port or (443 if request.url.scheme == "https" else 80)),
        "script_name": request.url.path,
        "get_data": dict(request.query_params),
        "post_data": post_data or {},
    }


def saml_claim_value(attributes: dict[str, Any], *names: str) -> str | None:
    wanted = {name.lower() for name in names}
    for key, values in attributes.items():
        tail = str(key).rstrip("/").rsplit("/", 1)[-1].lower()
        if tail in wanted:
            value = values[0] if isinstance(values, list) and values else values
            return str(value) if value is not None else None
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


def config_error() -> str | None:
    required = {
        "ENTRA_TENANT_ID": TENANT_ID,
        "ENTRA_APP1_CLIENT_ID": APP1_CLIENT_ID,
        "ENTRA_APP1_CLIENT_SECRET": APP1_CLIENT_SECRET,
        "ENTRA_APP1_SCOPE": APP1_SCOPE,
        "ENTRA_APP2_CLIENT_ID": APP2_CLIENT_ID,
        "ENTRA_APP2_SCOPE": APP2_SCOPE,
    }
    missing = [name for name, value in required.items() if not value]
    return "Missing configuration: " + ", ".join(missing) if missing else None


def msal_app() -> ConfidentialClientApplication:
    return ConfidentialClientApplication(
        APP1_CLIENT_ID,
        authority=AUTHORITY,
        client_credential=APP1_CLIENT_SECRET,
    )


def get_session(request: Request, create: bool = False) -> tuple[str | None, dict[str, Any] | None]:
    sid = request.cookies.get("poc_sid")
    session = SESSIONS.get(sid or "")
    if session and session.get("expires_at", 0) < time.time():
        SESSIONS.pop(sid or "", None)
        session = None
    if session:
        return sid, session
    if not create:
        return None, None
    sid = secrets.token_urlsafe(32)
    session = {"expires_at": time.time() + 8 * 60 * 60}
    SESSIONS[sid] = session
    return sid, session


def set_session_cookie(response: RedirectResponse | JSONResponse, sid: str) -> None:
    response.set_cookie(
        "poc_sid",
        sid,
        httponly=True,
        secure=False,  # localhost uses HTTP; use HTTPS and Secure cookies outside this local demo.
        samesite="lax",
        max_age=8 * 60 * 60,
    )


def validate_app1_token(token: str) -> dict[str, Any]:
    try:
        signing_key = JWKS.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=APP1_CLIENT_ID,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "aud", "iss", "tid", "oid"]},
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid Entra access token") from exc
    if claims.get("tid") != TENANT_ID or not claims.get("oid"):
        raise HTTPException(status_code=403, detail="Token is not from the configured tenant")
    if "access_as_user" not in claims.get("scp", "").split():
        raise HTTPException(status_code=403, detail="Token lacks the access_as_user delegated scope")
    return claims


def token_a_for_request(request: Request, authorization: str | None = None) -> tuple[str, dict[str, Any]]:
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        _, session = get_session(request)
        token = (session or {}).get("token_a", "")
    if not token:
        raise HTTPException(status_code=401, detail="Sign in at / or provide an App 1 bearer token")
    return token, validate_app1_token(token)


def acquire_app2_token(token_a: str) -> str:
    result = msal_app().acquire_token_on_behalf_of(
        user_assertion=token_a,
        scopes=[APP2_SCOPE],
    )
    token_b = result.get("access_token")
    if not token_b:
        description = result.get("error_description", result.get("error", "OBO token request failed"))
        raise HTTPException(status_code=502, detail=f"Entra OBO failed: {description}")
    return token_b


async def app2_request(method: str, path: str, token_b: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(timeout=20) as client:
        return await client.request(
            method,
            f"{APP2_BASE_URL}{path}",
            headers={"Authorization": f"Bearer {token_b}"},
            **kwargs,
        )


def raise_app2_error(response: httpx.Response) -> None:
    if response.is_error:
        content_type = response.headers.get("content-type", "unknown content type").split(";", 1)[0]
        try:
            payload = response.json()
        except Exception:
            detail = (
                f"Factory returned HTTP {response.status_code} with a non-JSON response "
                f"({content_type}); check the Factory terminal output and confirm it is running on port 8002."
            )
        else:
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if not detail:
                detail = f"Factory returned HTTP {response.status_code} without a JSON detail ({content_type})."
        raise HTTPException(status_code=response.status_code, detail=detail)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> str:
    _, session = get_session(request)
    if not session or not session.get("token_a"):
        saml_identity = (session or {}).get("saml_identity")
        if saml_identity:
            email = saml_identity.get("email") or saml_identity.get("name_id") or "Entra user"
            oauth_error = (session or {}).get("oauth_error")
            error_html = f'<p style="color:#a12525">OAuth continuation: {html_safe(str(oauth_error))}</p>' if oauth_error else ""
            return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Agentic Products · SAML</title>
            <style>body{{font:16px system-ui;max-width:850px;margin:4rem auto;padding:0 1rem;color:#152238}}button{{padding:.7rem 1rem;margin:.3rem}}.ok{{color:#16794b}}</style></head>
            <body><h1>Agentic Products · SAML complete</h1><p class="ok">Signed in as <b>{html_safe(str(email))}</b>.</p>
            <p>Next, we will obtain an Entra OAuth token for the OBO exchange. Entra may reuse this SAML session.</p>
            {error_html}
            <a href="/auth/login"><button>Continue to get Token A (OAuth)</button></a>
            <p><small>SAML assertion: aud={html_safe(str(saml_identity.get('aud')))} · tid={html_safe(str(saml_identity.get('tid')))} · oid={html_safe(str(saml_identity.get('oid')))}</small></p></body></html>"""
        return """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Agentic Products · OBO demo</title>
        <style>body{font:16px system-ui;max-width:850px;margin:4rem auto;padding:0 1rem}button{padding:.7rem 1rem;margin:.3rem}pre{white-space:pre-wrap;background:#f5f5f5;padding:1rem}</style></head>
        <body><h1>Agentic Products · OBO and GitHub</h1>
        <p>Sign in with SAML. The app then obtains a separate OAuth token to demonstrate OBO. MFA depends on your account and tenant policies.</p>
        <a href="/saml/login"><button>Sign in with Entra SAML</button></a>
        <p><a href="/auth/login">OIDC diagnostic sign-in (skip SAML)</a></p>
        <p><small>This local demo keeps the Entra token in server memory. You will need to sign in again after a restart.</small></p></body></html>"""
    user = session.get("user", {})
    username = user.get("preferred_username") or user.get("name") or "signed-in user"
    page = """<!doctype html>
    <html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Agentic Products · OBO demo</title>
    <style>
      :root{color-scheme:light;--ink:#152238;--muted:#62718a;--line:#d9e0e9;--green:#16794b;--green-bg:#e9f7ef;--gray:#7b8798;--gray-bg:#f1f3f6;--blue:#2857a4}
      *{box-sizing:border-box}body{font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--ink);background:#f6f8fb;margin:0}
      main{max-width:1050px;margin:0 auto;padding:48px 22px}h1{font-size:clamp(1.8rem,4vw,2.5rem);line-height:1.15;margin:0 0 10px}
      h2{font-size:1.15rem;margin:0}.eyebrow{text-transform:uppercase;letter-spacing:.12em;font-size:.75rem;color:var(--blue);font-weight:700}
      .lead{color:var(--muted);margin:0 0 28px}.identity{display:inline-flex;align-items:center;gap:8px;background:white;border:1px solid var(--line);border-radius:999px;padding:7px 12px;margin:0 0 25px;font-size:.9rem}
      .identity b{color:var(--ink)}.flow{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin:14px 0 24px}
      .step{position:relative;min-height:188px;padding:18px;border:1px solid var(--line);border-radius:16px;background:var(--gray-bg);color:var(--muted);transition:background .2s,border-color .2s,transform .2s}
      .step[data-state="connected"]{background:var(--green-bg);border-color:#9bd3b5;color:#315d46}.step-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}
      .num{font-size:.78rem;font-weight:700;color:var(--muted);background:#fff;border:1px solid var(--line);border-radius:50%;width:30px;height:30px;display:grid;place-items:center}
      .badge{display:inline-flex;align-items:center;gap:7px;border-radius:999px;padding:4px 9px;background:#e4e8ed;color:#687486;font-size:.76rem;font-weight:700}
      .step[data-state="connected"] .badge{background:#d1f0dc;color:var(--green)}.dot{width:8px;height:8px;border-radius:50%;background:currentColor}
      .step h2{color:var(--ink);margin:0 0 6px}.step p{font-size:.9rem;margin:0 0 10px}.step small{font-size:.78rem;color:var(--muted);overflow-wrap:anywhere}
      .claims{display:grid;grid-template-columns:36px minmax(0,1fr);gap:4px 8px;margin:8px 0 0;padding-top:8px;border-top:1px solid var(--line);font:.76rem ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}.claims dt{font-weight:700;color:var(--muted)}.claims dd{margin:0;color:var(--ink)}.claim-group{margin-top:10px}.claim-group strong{font-size:.78rem;color:var(--ink)}.claim-help{margin-top:10px;color:var(--muted);font-size:.78rem}.claim-help summary{cursor:pointer;color:var(--blue);font-weight:650}.claim-help p{margin:7px 0 0}
      .actions{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 20px}button,.button{font:inherit;font-weight:650;border:0;border-radius:9px;padding:10px 15px;cursor:pointer;background:var(--blue);color:white;text-decoration:none}
      button.secondary,.button.secondary{background:white;color:var(--ink);border:1px solid var(--line)}button:disabled{opacity:.55;cursor:wait}
      .panel{background:#fff;border:1px solid var(--line);border-radius:14px;padding:18px;min-height:74px;overflow:auto}.panel p{margin:0;color:var(--muted)}
      .sequence{background:#fff;border:1px solid var(--line);border-radius:14px;padding:18px;margin:20px 0}.sequence-head{display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap}.sequence-head p{margin:0;color:var(--muted);font-size:.85rem}.sequence-canvas{overflow:auto;margin-top:14px;border:1px solid #dce3ec;border-radius:10px;background:#fbfcfe}.sequence-svg{display:block;min-width:1100px;width:100%;font:13px system-ui,sans-serif}.sequence-svg .life{stroke:#cbd4df;stroke-dasharray:5 6}.sequence-svg .participant{fill:#eaf0f7;stroke:#cbd5e1;rx:10}.sequence-svg .participant-label{fill:#26364b;font-weight:700;text-anchor:middle}.sequence-svg .msg{cursor:pointer}.sequence-svg .msg-line{stroke:#9aa7b7;stroke-width:2}.sequence-svg .msg-head{fill:#9aa7b7}.sequence-svg .msg-label{fill:#526174;text-anchor:middle;font-size:12px}.sequence-svg .msg-num{fill:#687486}.sequence-svg .msg-num-text{fill:#fff;text-anchor:middle;font-weight:800;font-size:11px}.sequence-svg .msg[data-state="complete"] .msg-line{stroke:#24915a}.sequence-svg .msg[data-state="complete"] .msg-head{fill:#24915a}.sequence-svg .msg[data-state="complete"] .msg-label{fill:#17633c;font-weight:700}.sequence-svg .msg[data-state="complete"] .msg-num{fill:#24915a}.sequence-svg .msg[data-state="future"] .msg-line{stroke:#c2c8d1;stroke-dasharray:5 5}.sequence-svg .msg[data-state="future"] .msg-head{fill:#c2c8d1}.sequence-svg .msg[data-state="future"] .msg-label{fill:#8a929e}.sequence-svg .msg.selected .msg-line{stroke-width:4}.sequence-detail{margin-top:14px;padding:15px;border:1px solid #dce3ec;border-radius:10px;background:#fff}.sequence-detail h3{margin:0 0 12px;font-size:1rem}.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.detail-card{min-width:0;padding:12px;background:#f5f7fa;border-radius:8px}.detail-card strong{display:block;margin-bottom:6px;color:#526174;font-size:.76rem;text-transform:uppercase}.detail-card p{margin:0;overflow-wrap:anywhere;font-size:.85rem}.detail-card pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0 0;background:#142033;color:#e7edf7;padding:11px;border-radius:8px;font-size:.74rem}.sequence-detail[data-state="complete"]{border-color:#9bd3b5;background:#f6fcf8}.sequence-detail[data-state="inactive"]{border-color:#dce3ec}.entra-note{margin:14px 0;padding:13px 15px;border:1px solid #cbd8e7;border-radius:10px;background:#f5f8fc;color:#45546a;font-size:.88rem}.entra-note b{color:var(--ink)}.entra-note code{font-size:.82rem}.reset-note{color:var(--muted);font-size:.8rem;margin:9px 0 0}
      table{width:100%;border-collapse:collapse;margin-top:10px;font-size:.9rem}th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line)}th{color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.05em}
      a.repo{color:var(--blue);font-weight:650}.private{color:#795500}.public{color:var(--muted)}.footer{margin-top:20px;text-align:right}
      @media(max-width:920px){.flow{grid-template-columns:repeat(2,minmax(0,1fr))}}
      @media(max-width:620px){main{padding:30px 16px}.flow{grid-template-columns:1fr}.step{min-height:0}.detail-grid{grid-template-columns:1fr}}
    </style></head>
    <body><main>
      <div class="eyebrow">Agentic Platform · member demo</div>
      <h1>User access to GitHub</h1>
      <p class="lead">Sign in to Agentic Products with SAML. The app obtains Token A and automatically validates Factory through OBO (Token B). Then the GitHub button starts the connection in Factory; Factory stores the GitHub token linked to your Entra identity.</p>
      <p class="identity"><span class="dot" style="color:var(--green)"></span> Signed in as <b>__USERNAME__</b></p>

      <section class="sequence" aria-label="Diagrama de secuencia interactivo">
        <div class="sequence-head"><h2>Sequence diagram</h2><p>Select an arrow to inspect the request and response</p></div>
        <aside class="entra-note"><b>In short:</b> SAML signs you in to Agentic Products → Token A → OBO gets Token B for Factory → Factory validates the user → GitHub connects through Factory.</aside>
        <div class="sequence-canvas" id="sequence-events" aria-label="Participantes y mensajes"></div>
        <section class="sequence-detail" id="sequence-detail" aria-live="polite"><h3>Exchange details</h3><p>Select an arrow to see what is sent and what the service returns.</p></section>
      </section>


      <section class="flow" aria-label="Authentication and connection status">
        <article class="step" id="step-app1" data-state="inactive">
          <div class="step-top"><span class="num">1</span><span class="badge"><span class="dot"></span><span class="state-label">Checking</span></span></div>
          <h2>Agentic Products</h2><p>SAML + OAuth · Token A</p><small id="app1-detail">Checking the SAML identity and Token A.</small>
          <div class="claim-group"><strong>SAML identity · aud = Agentic Products Entity ID</strong><dl class="claims" id="app1-saml-claims"><dt>aud</dt><dd>Pending</dd><dt>tid</dt><dd>Pending</dd><dt>oid</dt><dd>Pending</dd></dl></div>
          <div class="claim-group"><strong>Token A · aud = Agentic Products API</strong><dl class="claims" id="app1-claims"><dt>aud</dt><dd>Pending</dd><dt>tid</dt><dd>Pending</dd><dt>oid</dt><dd>Pending</dd></dl></div>
          <details class="claim-help"><summary>Why are aud, tid, and oid shown twice?</summary><p><b>aud</b> identifies the credential's recipient: SAML is for the service provider, while Token A is for the Agentic Products API. <b>tid</b> identifies the tenant and <b>oid</b> identifies the user in that tenant. They repeat so we can verify both credentials belong to the same person. Token B keeps the same tid and oid, but its aud changes to the Factory API.</p></details>
        </article>
        <article class="step" id="step-factory" data-state="inactive">
          <div class="step-top"><span class="num">2</span><span class="badge"><span class="dot"></span><span class="state-label">Checking</span></span></div>
          <h2>Factory · API</h2><p>OBO · Token B</p><small id="factory-detail">Checking the Agentic Products API call to Factory.</small><div class="claim-group"><strong>Token B · aud = Factory API</strong><dl class="claims" id="factory-claims"><dt>aud</dt><dd>Pending</dd><dt>tid</dt><dd>Pending</dd><dt>oid</dt><dd>Pending</dd></dl></div>
        </article>
        <article class="step" id="step-github" data-state="inactive">
          <div class="step-top"><span class="num">3</span><span class="badge"><span class="dot"></span><span class="state-label">Not connected</span></span></div>
          <h2>GitHub App</h2><p>Installation repositories</p><small id="github-detail">Connect GitHub to authorize the selected repositories.</small>
        </article>
      </section>

      <div class="actions">
        <button type="button" id="connect-button" data-action="github" disabled>Connect GitHub · waiting for OBO validation</button>
        <button type="button" id="repos-button" class="secondary" data-action="repos" disabled>List my repositories</button>
        <button type="button" class="secondary" data-action="refresh">Refresh status</button>
        <button type="button" class="secondary" data-action="reset">Clear data and sign out</button>
      </div>
      <section class="panel" id="output" aria-live="polite"><p>Checking Factory and GitHub status…</p></section>
      <p class="reset-note">Reset clears the local Factory SAML session and stored GitHub token. It does not revoke the GitHub App authorization in your account.</p>
      <div class="footer"><a class="button secondary" href="/auth/logout">Sign out of Entra</a></div>
    </main>
    <script>
      const output=document.getElementById('output');
      const connectButton=document.getElementById('connect-button');
      const reposButton=document.getElementById('repos-button');
      connectButton.addEventListener('click',connectGithub);
      document.querySelector('[data-action="repos"]').addEventListener('click',loadRepos);
      document.querySelector('[data-action="refresh"]').addEventListener('click',refreshStatus);
      document.querySelector('[data-action="reset"]').addEventListener('click',resetDemo);
      window.addEventListener('error',event=>showMessage('Interface error: '+(event.message||'JavaScript is unavailable')));
      window.addEventListener('unhandledrejection',event=>showMessage('Interface error: '+String(event.reason?.message||event.reason||'unexpected failure')));
      function markStep(id,connected,label,detail){
        const card=document.getElementById('step-'+id);
        if(!card)return;
        card.dataset.state=connected?'connected':'inactive';
        card.querySelector('.state-label').textContent=label||(connected?'Connected':'Not connected');
        document.getElementById(id+'-detail').textContent=detail;
      }
      async function call(path,method='GET'){
        const response=await fetch(path,{method});
        let data;
        try{data=await response.json()}catch{throw new Error(`Unexpected response from ${path} (HTTP ${response.status})`)}
        if(!response.ok)throw new Error(data.detail||JSON.stringify(data));
        return data;
      }
      function renderClaims(elementId,claims){
        const target=document.getElementById(elementId);target.replaceChildren();
        for(const key of ['aud','tid','oid']){
          const label=document.createElement('dt');label.textContent=key;
          const value=document.createElement('dd');value.textContent=claims?.[key]===undefined?'—':String(claims[key]);
          target.append(label,value);
        }
      }
      function showMessage(message){output.replaceChildren();const p=document.createElement('p');p.textContent=message;output.appendChild(p)}
      const flowRuntime={githubStarted:false,repositoriesListed:false};
      let latestStatus={};
      function renderSequence(data={}){
        latestStatus=data;
        const app1=data.app1_claims||{},factory=data.factory_claims||{},app1Saml=data.app1_saml||{};
        const fmt=claims=>'aud='+JSON.stringify(claims?.aud??'—')+' · tid='+(claims?.tid||'—')+' · oid='+(claims?.oid||'—');
        const hasTokenA=!!app1.tid,hasTokenB=!!factory.tid,hasApp1Saml=!!app1Saml.tid;
        const hasGithub=!!data.connected||flowRuntime.githubStarted||new URLSearchParams(window.location.search).has('github');
        const api='curl -H "Authorization: Bearer <TOKEN_B_REDACTED>"';
        const messages=[
          {phase:'1 · Sign-in: SAML + OBO',from:1,to:0,ok:hasApp1Saml,label:'SAML · AuthnRequest / SAMLResponse',request:'Agentic Products redirects the user to Entra ID to sign in. Entra returns a signed SAML assertion to the local ACS.',response:hasApp1Saml?'Assertion validated for '+fmt(app1Saml):'Pending: sign in with SAML. The Entity ID and ACS identify the service provider.',curl:'GET "__APP1_BASE_URL__/saml/login"\\nSAMLResponse → POST "__APP1_SAML_ACS_URL__"'},
          {phase:'1 · Sign-in: SAML + OBO',from:1,to:0,ok:hasTokenA,label:'OAuth · acquire Token A',request:'Agentic Products requests a delegated token for its own API (authorization code + PKCE).',response:hasTokenA?'Token A received. '+fmt(app1):'Pending: Token A for the Agentic Products API.',curl:'GET /oauth2/v2.0/authorize · client_id=<APP1> · redirect_uri=<APP1_CALLBACK>\\nPOST /oauth2/v2.0/token → access_token=<TOKEN_A_REDACTED>'},
          {phase:'1 · Sign-in: SAML + OBO',from:1,to:2,ok:hasTokenB,label:'OBO · exchange Token A for Token B',request:'Agentic Products sends Token A to Entra and requests a delegated token for the Factory audience.',response:hasTokenB?'Token B issued for Factory. '+fmt(factory):'Pending: OBO for api://<FACTORY>/access_as_user.',curl:'POST /oauth2/v2.0/token · requested_token_use=on_behalf_of\\nassertion=<TOKEN_A_REDACTED> · scope=api://<FACTORY>/access_as_user'},
          {phase:'1 · Sign-in: SAML + OBO',from:1,to:3,ok:hasTokenB,label:'Validate Token B · GET /identity/validate',request:'Agentic Products calls the protected Factory API with Token B.',response:hasTokenB?'Factory validated the signature, audience, scope, and matching tid + oid.':'Pending: Factory validates Token B before enabling GitHub.',curl:api+' "__APP2_BASE_URL__/identity/validate"'},
          {phase:'2 · Connect GitHub',from:1,to:3,ok:hasGithub,label:'Start GitHub OAuth · POST /github/connect/start',request:'Agentic Products sends Token B to Factory. Factory creates a one-time state value bound to tid + oid.',response:hasGithub?'Factory returns the GitHub authorization URL.':'Click “Connect GitHub” to start this step.',curl:api+' -X POST "__APP2_BASE_URL__/github/connect/start"'},
          {phase:'2 · Connect GitHub',from:3,to:4,ok:!!data.connected,label:'Authorize the GitHub App',request:'The browser opens GitHub authorization; the user confirms the account and requested access.',response:data.connected?'GitHub authorized the account '+data.github_login+'.':'Pending: user authorization on GitHub.',curl:'GET https://github.com/login/oauth/authorize?client_id=<GITHUB_APP_CLIENT_ID>&state=<REDACTED>'},
          {phase:'2 · Connect GitHub',from:4,to:3,ok:!!data.connected,label:'Callback · Factory stores the credential',request:'GitHub redirects to Factory with code + state. Factory exchanges the code for the user credential.',response:data.connected?'Encrypted token stored for tid + oid for '+data.github_login+'.':'Pending: callback and encrypted storage in Factory.',curl:'GET "__APP2_BASE_URL__/github/callback?code=<REDACTED>&state=<REDACTED>"'},
          {phase:'3 · List repositories',from:1,to:3,ok:flowRuntime.repositoriesListed,label:'List repositories · GET /github/repos',request:'Agentic Products calls Factory with Token B. Factory looks up the GitHub connection for tid + oid.',response:flowRuntime.repositoriesListed?'Factory returns the authorized list to Agentic Products.':'Click “List my repositories” after connecting GitHub.',curl:api+' "__APP2_BASE_URL__/github/repos"'},
          {phase:'3 · List repositories',from:3,to:4,ok:flowRuntime.repositoriesListed,label:'GitHub request · installation repositories',request:'Factory finds this GitHub App’s installations and requests the repositories allowed by each installation.',response:flowRuntime.repositoriesListed?'GitHub returns only repositories granted to this installation.':'Pending: Factory calls the installation/repositories endpoints.',curl:'GET https://api.github.com/user/installations\\nGET https://api.github.com/user/installations/{installation_id}/repositories · Bearer <GITHUB_TOKEN_REDACTED>'},
          {phase:'3 · List repositories',from:3,to:1,ok:flowRuntime.repositoriesListed,label:'JSON response with repositories',request:'Factory responds to Agentic Products with the permitted repository data.',response:flowRuntime.repositoriesListed?'Agentic Products displays the repositories on the page.':'Pending: response from Factory.',curl:'HTTP 200 · {"repositories":[...],"count":N}'}
        ];
        const host=document.getElementById('sequence-events');host.replaceChildren();
        const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('class','sequence-svg');svg.setAttribute('viewBox','0 0 1200 '+(120+messages.length*76));svg.setAttribute('role','img');svg.setAttribute('aria-label','Interactive authentication and GitHub connection sequence');
        const ns='http://www.w3.org/2000/svg',xs=[120,360,600,840,1080],names=['Entra ID · App 1','API · Agentic Products','Entra ID · App 2','API · Factory','GitHub'];
        for(let i=0;i<xs.length;i++){const box=document.createElementNS(ns,'rect');box.setAttribute('class','participant');box.setAttribute('x',xs[i]-92);box.setAttribute('y','18');box.setAttribute('width','184');box.setAttribute('height','48');svg.appendChild(box);const name=document.createElementNS(ns,'text');name.setAttribute('class','participant-label');name.setAttribute('x',xs[i]);name.setAttribute('y','47');name.textContent=names[i];svg.appendChild(name);const life=document.createElementNS(ns,'line');life.setAttribute('class','life');life.setAttribute('x1',xs[i]);life.setAttribute('x2',xs[i]);life.setAttribute('y1','66');life.setAttribute('y2',String(100+messages.length*76));svg.appendChild(life)}
        let previousPhase='';
        messages.forEach((message,index)=>{
          const y=112+index*76,state=message.ok?'complete':'inactive';
          if(message.phase!==previousPhase){const phase=document.createElementNS(ns,'text');phase.setAttribute('x','18');phase.setAttribute('y',String(y-19));phase.setAttribute('fill','#59677a');phase.setAttribute('font-size','12');phase.setAttribute('font-weight','700');phase.textContent=message.phase;svg.appendChild(phase);previousPhase=message.phase}
          const group=document.createElementNS(ns,'g');group.setAttribute('class','msg');group.setAttribute('data-state',state);group.setAttribute('tabindex','0');group.setAttribute('role','button');group.setAttribute('aria-label',(index+1)+'. '+message.label);
          const x1=xs[message.from],x2=xs[message.to],direction=x2>=x1?1:-1;
          const line=document.createElementNS(ns,'line');line.setAttribute('class','msg-line');line.setAttribute('x1',x1);line.setAttribute('x2',x2-direction*8);line.setAttribute('y1',y);line.setAttribute('y2',y);group.appendChild(line);
          const head=document.createElementNS(ns,'path');head.setAttribute('class','msg-head');head.setAttribute('d',direction===1?`M ${x2-8} ${y-5} L ${x2} ${y} L ${x2-8} ${y+5} Z`:`M ${x2+8} ${y-5} L ${x2} ${y} L ${x2+8} ${y+5} Z`);group.appendChild(head);
          const circle=document.createElementNS(ns,'circle');circle.setAttribute('class','msg-num');circle.setAttribute('cx',(x1+x2)/2);circle.setAttribute('cy',y);circle.setAttribute('r','11');group.appendChild(circle);
          const number=document.createElementNS(ns,'text');number.setAttribute('class','msg-num-text');number.setAttribute('x',(x1+x2)/2);number.setAttribute('y',y+4);number.textContent=String(index+1);group.appendChild(number);
          const label=document.createElementNS(ns,'text');label.setAttribute('class','msg-label');label.setAttribute('x',(x1+x2)/2);label.setAttribute('y',y-12);label.textContent=message.label;group.appendChild(label);
          group.addEventListener('click',()=>showMessageDetail(message,index,group));group.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();showMessageDetail(message,index,group)}});svg.appendChild(group);
        });
        host.appendChild(svg);
        showMessageDetail(messages[0],0,svg.querySelector('.msg'));
      }
      function showMessageDetail(message,index,group){
        document.querySelectorAll('.msg.selected').forEach(node=>node.classList.remove('selected'));if(group)group.classList.add('selected');
        const panel=document.getElementById('sequence-detail');panel.dataset.selected='true';panel.dataset.state=message.ok?'complete':'inactive';panel.replaceChildren();
        const heading=document.createElement('h3');heading.textContent=(index+1)+' · '+message.label;panel.appendChild(heading);
        const grid=document.createElement('div');grid.className='detail-grid';
        for(const [title,value] of [['Request',message.request],['Response',message.response]]){const card=document.createElement('div');card.className='detail-card';const strong=document.createElement('strong');strong.textContent=title;const p=document.createElement('p');p.textContent=value;card.append(strong,p);grid.appendChild(card)}
        const curl=document.createElement('div');curl.className='detail-card';const curlTitle=document.createElement('strong');curlTitle.textContent='Example request (cURL / protocol)';const pre=document.createElement('pre');pre.textContent=message.curl;curl.append(curlTitle,pre);grid.appendChild(curl);panel.appendChild(grid);
      }
      function showRepositories(data){
        output.replaceChildren();
        const summary=document.createElement('p');
        summary.textContent=`${data.count} allowed repositories for ${data.github_login}.`;
        output.appendChild(summary);
        if(!data.repositories.length)return;
        const table=document.createElement('table');
        table.innerHTML='<thead><tr><th>Repository</th><th>Visibility</th><th>Default branch</th></tr></thead>';
        const body=document.createElement('tbody');
        for(const repo of data.repositories){
          const row=document.createElement('tr');
          const nameCell=document.createElement('td');
          const link=document.createElement('a');link.className='repo';link.href=repo.html_url;link.target='_blank';link.rel='noopener noreferrer';link.textContent=repo.full_name;
          nameCell.appendChild(link);
          const visibility=document.createElement('td');visibility.className=repo.private?'private':'public';visibility.textContent=repo.private?'Private':'Public';
          const branch=document.createElement('td');branch.textContent=repo.default_branch||'—';
          row.append(nameCell,visibility,branch);body.appendChild(row);
        }
        table.appendChild(body);output.appendChild(table);
      }
      async function refreshStatus(){
        showMessage('Checking the OBO flow and GitHub connection…');
        try{
          const data=await call('/api/github/status');
          markStep('app1',!!data.app1_saml?.tid, data.app1_saml?.tid?'SAML + Token A':'SAML not started', data.app1_saml?.tid?'SAML validated and Token A acquired for Agentic Products.':'This session was started through the OIDC diagnostic sign-in.');
          markStep('factory',true,'OBO validated','Factory validated Token B and the user identity.');
          renderClaims('app1-claims',data.app1_claims);
          renderClaims('app1-saml-claims',data.app1_saml);
          renderClaims('factory-claims',data.factory_claims);
          connectButton.disabled=!!data.connected;
          connectButton.textContent=data.connected?'GitHub connected':'Connect GitHub';
          reposButton.disabled=!data.connected;
          markStep('github',data.connected,data.connected?'Connected':'Not connected',data.connected?`Connected as ${data.github_login}. The repositories selected for the installation apply.`:'Authorize GitHub to continue.');
          showMessage(data.connected?`Factory is available. GitHub is connected as ${data.github_login}.`:'Factory is available. Connect GitHub to continue.');
          renderSequence(data);
        }catch(error){
          connectButton.disabled=true;
          connectButton.textContent='Connect GitHub · OBO pending';
          reposButton.disabled=true;
          markStep('factory',false,'OBO pending','Factory has not validated Token B yet.');
          markStep('github',false,'Not connected','Connect GitHub after Factory is available.');
          showMessage(error.message);
          renderSequence({});
        }
      }
      async function connectGithub(){
        connectButton.disabled=true;connectButton.textContent='Opening GitHub…';
        markStep('github',false,'Starting','Waiting for GitHub authorization…');
        showMessage('Agentic Products will call the Factory API with Token B and redirect you to GitHub.');
        try{
          const data=await call('/api/github/connect','POST');
          flowRuntime.githubStarted=true;renderSequence(latestStatus);
          markStep('factory',true,'OBO validated','Factory validated Token B and the user identity.');
          window.location=data.authorization_url;
        }catch(error){
          showMessage(error.message);connectButton.disabled=false;connectButton.textContent='Connect GitHub';
          await refreshStatus();
        }
      }
      async function loadRepos(){
        showMessage('Requesting the repositories allowed by this installation from Factory…');
        try{
          const data=await call('/api/github/repos');
          flowRuntime.repositoriesListed=true;renderSequence(latestStatus);
          markStep('factory',true,'OBO validated','Factory validated Token B and the user identity.');
          markStep('github',true,'Connected',`Connected as ${data.github_login}; repositories are scoped to the installation.`);
          showRepositories(data);
        }catch(error){showMessage(error.message);}
      }
      async function resetDemo(){
        if(!window.confirm('Clear the Factory SAML session and stored GitHub token, then sign out of Entra?'))return;
        showMessage('Clearing this user’s local data…');
        try{
          const data=await call('/demo/reset','POST');
          flowRuntime.githubStarted=false;flowRuntime.repositoriesListed=false;
          showMessage(data.note+' Redirecting to sign out…');
          const returnTo='__APP1_BASE_URL__/auth/logout?reset=1';
          window.location='__APP2_BASE_URL__/demo/logout?'+new URLSearchParams({return_to:returnTo});
        }catch(error){showMessage('Could not complete the reset: '+error.message);}
      }
      const params=new URLSearchParams(window.location.search);
      if(params.has('github')){showMessage(params.get('github')==='connected'?'GitHub authorization completed. Verifying the connection…':'GitHub authorization was not completed.');history.replaceState(null,'','/');}
      if(params.has('saml')){showMessage(params.get('saml')==='connected'?'SAML sign-in completed. Comparing SAML and OBO identities…':'SAML sign-in was not completed.');history.replaceState(null,'','/');}
      if(params.has('reset'))showMessage('The demo has been reset. You can start again.');
      refreshStatus();
    </script></body></html>"""
    return page.replace("__USERNAME__", html_safe(username)).replace("__APP2_BASE_URL__", APP2_BASE_URL).replace("__APP1_BASE_URL__", APP1_BASE_URL).replace("__APP1_SAML_ACS_URL__", APP1_SAML_ACS_URL)


def html_safe(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#x27;")


@app.get("/saml/metadata")
async def agentic_saml_metadata():
    _, settings = saml_settings()
    metadata = settings.get_sp_metadata()
    errors = settings.validate_metadata(metadata)
    if errors:
        raise HTTPException(status_code=500, detail="Agentic Products SAML SP metadata is invalid: " + ", ".join(errors))
    return Response(content=metadata, media_type="application/samlmetadata+xml")


@app.get("/saml/login")
async def agentic_saml_login(request: Request):
    if error := config_error():
        raise HTTPException(status_code=500, detail=error)
    OneLogin_Saml2_Auth, settings = saml_settings()
    for key, pending in list(SAML_PENDING.items()):
        if pending.get("expires_at", 0) < time.time():
            SAML_PENDING.pop(key, None)
    relay_state = secrets.token_urlsafe(32)
    auth = OneLogin_Saml2_Auth(saml_request_data(request), old_settings=settings)
    destination = auth.login(return_to=relay_state)
    request_id = auth.get_last_request_id()
    if not request_id:
        raise HTTPException(status_code=500, detail="SAML toolkit did not create an Agentic Products authentication request")
    SAML_PENDING[relay_state] = {"request_id": request_id, "expires_at": time.time() + 600}
    return RedirectResponse(destination)


@app.post("/saml/acs")
async def agentic_saml_acs(request: Request):
    body = await request.body()
    if len(body) > 2_000_000:
        raise HTTPException(status_code=413, detail="SAML response is too large")
    try:
        form_data = {key: values[0] for key, values in parse_qs(body.decode("utf-8"), keep_blank_values=True).items()}
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid SAML form response") from exc
    if not form_data.get("SAMLResponse") or not form_data.get("RelayState"):
        raise HTTPException(status_code=400, detail="SAMLResponse and RelayState are required")
    pending = SAML_PENDING.pop(form_data["RelayState"], None)
    if not pending or pending["expires_at"] < time.time():
        raise HTTPException(status_code=400, detail="Agentic Products SAML login state expired; start again from the home page")
    OneLogin_Saml2_Auth, settings = saml_settings()
    auth = OneLogin_Saml2_Auth(saml_request_data(request, form_data), old_settings=settings)
    auth.process_response(request_id=pending["request_id"])
    if auth.get_errors() or not auth.is_authenticated():
        errors = ", ".join(auth.get_errors()) or "not_authenticated"
        reason = auth.get_last_error_reason() or "No additional reason was provided by python3-saml."
        raise HTTPException(
            status_code=401,
            detail=f"Agentic Products SAML validation failed (errors: {errors}; reason: {reason}). Check the Entity ID, ACS, metadata, and signature settings.",
        )
    attributes = auth.get_attributes()
    tenant_id = saml_claim_value(attributes, "tenantid", "tid")
    object_id = saml_claim_value(attributes, "objectidentifier", "oid")
    audiences = saml_audiences(auth)
    if tenant_id != TENANT_ID or not object_id or APP1_SAML_SP_ENTITY_ID not in audiences:
        raise HTTPException(status_code=403, detail="SAML assertion has the wrong audience, tenant, or missing user object ID")

    sid = secrets.token_urlsafe(32)
    saml_identity = {
        "aud": audiences,
        "tid": tenant_id,
        "oid": object_id,
        "name_id": auth.get_nameid(),
        "email": saml_claim_value(attributes, "emailaddress", "email", "upn", "name"),
    }
    session = {"saml_identity": saml_identity, "expires_at": time.time() + 8 * 60 * 60}
    SESSIONS[sid] = session
    flow = msal_app().initiate_auth_code_flow(
        scopes=[APP1_SCOPE],
        redirect_uri=APP1_REDIRECT_URI,
        prompt="none",
    )
    if "auth_uri" not in flow:
        session["oauth_error"] = flow.get("error_description", "Could not start OAuth token acquisition")
        response = RedirectResponse(f"{APP1_BASE_URL}/?oauth=required")
    else:
        session["auth_flow"] = flow
        response = RedirectResponse(flow["auth_uri"])
    set_session_cookie(response, sid)
    return response


@app.get("/auth/login")
async def login(request: Request):
    if error := config_error():
        raise HTTPException(status_code=500, detail=error)
    sid, session = get_session(request, create=True)
    flow = msal_app().initiate_auth_code_flow(
        scopes=[APP1_SCOPE],
        redirect_uri=APP1_REDIRECT_URI,
        prompt="select_account",
    )
    if "auth_uri" not in flow:
        raise HTTPException(status_code=500, detail=flow.get("error_description", "Could not start Entra sign-in"))
    session["auth_flow"] = flow
    response = RedirectResponse(flow["auth_uri"])
    set_session_cookie(response, sid or "")
    return response


@app.get("/auth/callback")
async def auth_callback(request: Request):
    sid, session = get_session(request)
    flow = (session or {}).pop("auth_flow", None)
    if not sid or not session or not flow:
        raise HTTPException(status_code=400, detail="Login state expired. Start again from the home page.")
    try:
        result = msal_app().acquire_token_by_auth_code_flow(flow, dict(request.query_params))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid sign-in state") from exc
    if not result.get("access_token"):
        if session.get("saml_identity"):
            session["oauth_error"] = result.get("error_description", result.get("error", "Entra OAuth token acquisition failed"))
            return RedirectResponse(f"{APP1_BASE_URL}/?oauth=required")
        detail = result.get("error_description", result.get("error", "Entra sign-in failed"))
        raise HTTPException(status_code=401, detail=detail)
    claims = validate_app1_token(result["access_token"])
    saml_identity = session.get("saml_identity")
    if saml_identity and (claims.get("tid") != saml_identity.get("tid") or claims.get("oid") != saml_identity.get("oid")):
        raise HTTPException(status_code=403, detail="SAML and OAuth sign-ins resolved to different Entra users; restart and sign in with the same account")
    session.update({"token_a": result["access_token"], "user": claims, "expires_at": time.time() + 8 * 60 * 60})
    return RedirectResponse(APP1_BASE_URL)


@app.get("/auth/logout")
async def logout(request: Request, reset: str | None = None):
    sid, _ = get_session(request)
    if sid:
        SESSIONS.pop(sid, None)
    return_to = f"{APP1_BASE_URL}/?reset=complete" if reset else APP1_BASE_URL
    response = RedirectResponse(f"{AUTHORITY}/oauth2/v2.0/logout?{urlencode({'post_logout_redirect_uri': return_to})}")
    response.delete_cookie("poc_sid")
    return response


@app.post("/demo/reset")
async def reset_demo(request: Request, authorization: str | None = Header(default=None)):
    token_a, _ = token_a_for_request(request, authorization)
    token_b = acquire_app2_token(token_a)
    response = await app2_request("POST", "/demo/reset", token_b)
    raise_app2_error(response)
    result = JSONResponse(response.json())
    sid = request.cookies.get("poc_sid")
    if sid:
        SESSIONS.pop(sid, None)
    SAML_PENDING.clear()
    result.delete_cookie("poc_sid")
    return result


@app.post("/api/github/connect")
async def github_connect(request: Request, authorization: str | None = Header(default=None)):
    token_a, _ = token_a_for_request(request, authorization)
    token_b = acquire_app2_token(token_a)
    response = await app2_request("POST", "/github/connect/start", token_b, json={"return_to": APP1_BASE_URL})
    raise_app2_error(response)
    return response.json()


@app.get("/api/github/status")
async def github_status(request: Request, authorization: str | None = Header(default=None)):
    token_a, claims_a = token_a_for_request(request, authorization)
    token_b = acquire_app2_token(token_a)
    response = await app2_request("GET", "/github/status", token_b)
    raise_app2_error(response)
    factory_response = await app2_request("GET", "/identity/validate", token_b)
    raise_app2_error(factory_response)
    factory_claims = factory_response.json().get("claims", {})
    if factory_claims.get("tid") != claims_a.get("tid") or factory_claims.get("oid") != claims_a.get("oid"):
        raise HTTPException(status_code=403, detail="Factory validated Token B, but its tid/oid do not match the Agentic Products user")
    result = response.json()
    result["app1_claims"] = {key: claims_a.get(key) for key in ("aud", "tid", "oid")}
    _, session = get_session(request)
    app1_saml = (session or {}).get("saml_identity") or {}
    result["app1_saml"] = {key: app1_saml.get(key) for key in ("aud", "tid", "oid")}
    result["factory_claims"] = factory_claims
    return result


@app.get("/api/github/repos")
async def github_repos(request: Request, authorization: str | None = Header(default=None)):
    token_a, _ = token_a_for_request(request, authorization)
    token_b = acquire_app2_token(token_a)
    response = await app2_request("GET", "/github/repos", token_b)
    raise_app2_error(response)
    return response.json()


@app.get("/health")
async def health():
    return {"status": "ok", "application": "agentic_products"}
