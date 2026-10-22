# Configuration reference

For a copy-ready corporate admin request covering both app registrations, OBO consent, local and hosted SAML Enterprise applications, claims, callback URLs, and the values to return, see the [Entra administrator handoff](entra-admin-handoff.md).

## Software

The project uses Python 3.13, managed by `uv`. In this MacPorts environment, `port` reports `uv @0.12.3`, `python313 @3.13.15`, and `azure-cli @2.89.1` installed. Python's system `/usr/bin/python3` is 3.9.6; use `uv run` so it selects the project Python instead.

Dependencies are declared in `pyproject.toml`: FastAPI/Uvicorn, MSAL Python, HTTPX, PyJWT with cryptography, python-dotenv, cryptography/Fernet, and `python3-saml` for the local SAML service provider. Resolve and install them with `uv lock && uv sync --python 3.13` when network access is available.

## Entra App 1: Agentic Products

Register a single-tenant app named `Agentic Products` in tenant `662f928d-ee5c-4b0a-a0f7-0072c9c0d1bd`.

| Setting | Value |
| --- | --- |
| Platform | Web |
| Web redirect URI | `http://localhost:8001/auth/callback` |
| Application ID URI | `api://<APP1_CLIENT_ID>` |
| Exposed delegated scope | `access_as_user` |
| OAuth access token version | 2 |
| Credential | Client secret, stored only in `.env` |
| Public client flow | Enabled for the terminal device-code client |

The combined App 1 registration acts as both the first API/resource and the local browser host. It requests its own `access_as_user` scope to obtain Token A, validates Token A, and uses the same App 1 client credential to perform OBO to App 2. This keeps the two registration design in the supplied sequence diagram. The standard Microsoft quickstart shows separate client/API registrations; this POC combines those duties in App 1 to preserve the diagram's App1→App2 exchange.

App 1 also has a separate local **SAML Enterprise application** for browser SSO. Keep App 1's OAuth Web redirect below for Token A, and configure this independent SAML SP on the Enterprise application:

| Entra Basic SAML setting | Local Agentic Products value |
| --- | --- |
| Identifier (Entity ID) | `http://localhost:8001/saml/metadata` |
| Reply URL (ACS) | `http://localhost:8001/saml/acs` |
| Sign-on URL | `http://localhost:8001/saml/login` |

Set `APP1_SAML_IDP_METADATA_URL` to the **App Federation Metadata Url** shown on this specific Agentic Products Enterprise application's SAML page. This URL identifies the app's signing certificate; do not use the tenant-wide federation metadata endpoint. Factory has its own signing certificate and must use its own App Federation Metadata Url in `SAML_IDP_METADATA_URL`. The browser SAML response establishes Agentic Products' SAML session. The app then starts an OAuth authorization-code flow for its own API scope to get Token A; it first tries `prompt=none` so Entra can reuse the browser SSO session, and offers an interactive continuation if Entra requires it.

## Entra App 2: Factory OAuth client and OBO API

Register a single-tenant app named `Factory` in the same tenant.

| Setting | Value |
| --- | --- |
| Platform | Web, plus Expose an API |
| Web redirect URI | `http://localhost:8002/auth/callback` |
| Application ID URI | `api://<APP2_CLIENT_ID>` |
| Exposed delegated scope | `access_as_user` |
| OAuth access token version | 2 |
| Credential | Client secret stored in `.env`; used by Factory's interactive OAuth code flow |

The Factory registration serves two roles: it is the OAuth web client for Factory's own interactive sign-in, and it exposes the API audience/scope requested by Agentic Products during OBO. The OAuth callback is `http://localhost:8002/auth/callback`. The OBO grant itself does not use a redirect URI. Do not add the SAML ACS or GitHub callback as OAuth redirect URIs:

- Local SAML POC: add `http://localhost:8002/saml/acs` as the **Reply URL (ACS URL)** under **Enterprise applications → Local Factory SAML POC → Single sign-on → SAML → Basic SAML Configuration**.
- GitHub App: set `http://localhost:8002/github/callback` in the GitHub App's OAuth callback configuration.

For the hosted Factory product SSO, use the Entity ID and ACS URL from Factory's own setup guide on its separate Enterprise application. Those values are not the localhost POC callback.

In App 1's API permissions, add **My APIs → Factory → Delegated permissions → access_as_user**. Consent must be granted in the tenant. If user consent is disabled or Entra requires admin consent, an appropriately privileged tenant administrator must grant it.

## Factory product SSO: SAML Enterprise application

The Factory organization setup guide in `setup.factory.ai` configures **SAML SSO** for the Factory product. This is a different Entra object and protocol from the `factory` app registration above, which exposes the API audience and delegated scope used by OBO. Keep both configurations: SAML signs a user into the Factory product; OBO lets `agentic_products` call the `factory` API as that user.

Follow the Factory setup wizard's **Entra ID (Azure AD) SAML** guide. In the screenshots you shared, the Factory wizard supplies an Entity ID based on that connection and an ACS under `https://auth.workos.com/sso/saml/acs/<connection-id>`. Use the actual values displayed in your wizard; they are specific to your Factory organization and connection. Configure:

1. In Entra admin center, go to **Enterprise applications → New application → Create your own application**. Name it clearly, for example `Factory SAML SSO`, and choose **Integrate any other application you don't find in the gallery (Non-gallery)**.
2. Open the new enterprise application and choose **Single sign-on → SAML**.
3. In **Basic SAML Configuration**, copy the **Identifier (Entity ID)** and **Reply URL (ACS URL)** from the Factory setup wizard for your organization/connection. These are connection-specific; do not use the local Python POC ACS `http://localhost:8002/saml/acs` here.
4. Complete the SAML attributes/claims and user assignment steps in the Factory wizard. Assign the test user (or group) to this enterprise application.
5. In Entra's **SAML Signing Certificate** section, copy the **App Federation Metadata URL** and paste it into the Factory wizard's identity-provider metadata step; then use its **Test Single Sign-On** step.

The SAML app's Entity ID, ACS URL, and metadata describe the Factory product's SAML trust. They do not replace `api://<APP2_CLIENT_ID>`, `access_as_user`, or App 1's delegated permission/consent configuration for OBO. The local demo's `/identity/validate` endpoint validates the OBO access token and shows its JWT `aud`, `tid`, and `oid` claims; it does not receive or inspect the Factory product's SAML assertion.

### Local POC Factory SAML values

The Python `factory` service also implements a separate local SAML service provider so the browser demo can exercise an actual Entra SAML assertion and compare it with the OBO identity. Create a **second** non-gallery Enterprise application for this local POC, rather than reusing the hosted Factory/WorkOS enterprise application:

| Entra Basic SAML setting | Local POC value |
| --- | --- |
| Identifier (Entity ID) | `http://localhost:8002/saml/metadata` |
| Reply URL (ACS) | `http://localhost:8002/saml/acs` |
| Sign-on URL | `http://localhost:8002/saml/login` |

Keep the account on `localhost` for the full browser flow. Do not mix `localhost` and `127.0.0.1`; the local session cookies are host-bound.

Both local SAML handlers require and validate a signed Entra assertion (Entra may leave the outer SAML Response unsigned). They also check response/request correlation, expiry, configured audience, tenant ID, and user object ID. Entra's `tid` and `oid` SAML attributes use Microsoft identity claim URIs ending in `tenantid` and `objectidentifier`.

## GitHub App

Create a GitHub App under the personal GitHub account. You can use the automated [GitHub manifest setup](automated-setup.md) or **Settings → Developer settings → GitHub Apps → New GitHub App**.

| Setting | Value |
| --- | --- |
| Homepage URL | `http://localhost:8001` |
| Callback URL | `http://localhost:8002/github/callback` |
| Repository permission | Metadata: Read-only |
| Webhook | Disabled for this POC |
| Installation | Personal account; select **Only select repositories** and choose the repositories Factory may list |
| User tokens | Leave expiring user authorization enabled |
| Private key | Generated and saved locally by the manifest flow; retained for future installation-token work |

The GitHub App Client ID and Client Secret are used for the OAuth callback and refresh token exchange. Do not generate or supply the GitHub App private key for this user-token POC.

## `.env` values

Copy `.env.example` to `.env`, then set:

| Variable | Value |
| --- | --- |
| `ENTRA_TENANT_ID` | `662f928d-ee5c-4b0a-a0f7-0072c9c0d1bd` |
| `ENTRA_APP1_CLIENT_ID` | App 1 Application (client) ID |
| `ENTRA_APP1_CLIENT_SECRET` | App 1 secret **value** |
| `ENTRA_APP1_SCOPE` | `api://<APP1_CLIENT_ID>/access_as_user` |
| `ENTRA_APP2_CLIENT_ID` | App 2 Application (client) ID |
| `ENTRA_APP2_CLIENT_SECRET` | App 2 secret **value**, used for Factory's OAuth callback flow |
| `ENTRA_APP2_SCOPE` | `api://<APP2_CLIENT_ID>/access_as_user` |
| `APP2_OAUTH_REDIRECT_URI` | `http://localhost:8002/auth/callback` |
| `SAML_SP_ENTITY_ID` | Local SP Entity ID above |
| `SAML_ACS_URL` | Local ACS URL above |
| `SAML_IDP_METADATA_URL` | **App Federation Metadata Url** from the local Factory SAML Enterprise application |
| `APP1_SAML_SP_ENTITY_ID` | Local Agentic Products SP Entity ID above |
| `APP1_SAML_ACS_URL` | Local Agentic Products ACS URL above |
| `APP1_SAML_IDP_METADATA_URL` | **App Federation Metadata Url** from the local Agentic Products SAML Enterprise application |
| `GITHUB_APP_CLIENT_ID` | GitHub App Client ID |
| `GITHUB_APP_CLIENT_SECRET` | GitHub App Client Secret |
| `GITHUB_APP_ID` | Numeric GitHub App ID; used to select this app's installations |
| `TOKEN_ENCRYPTION_KEY` | Fernet key generated locally, see `docs/setup.md` |

`APP1_BASE_URL`, `APP2_BASE_URL`, redirect URI, callback URL, and token database have localhost defaults in `.env.example`. `.env`, `.venv`, and the token database are git-ignored.

## Route map

| Service | Route | Purpose |
| --- | --- | --- |
| `agentic_products` | `/` | Browser UI |
| `agentic_products` | `/auth/login`, `/auth/callback`, `/auth/logout` | Entra authorization-code sign-in |
| `agentic_products` | `POST /api/github/connect` | Performs OBO, asks Factory to start GitHub authorization |
| `agentic_products` | `GET /api/github/status` | Performs OBO and retrieves link status |
| `agentic_products` | `GET /api/github/repos` | Performs OBO and retrieves repository list |
| `agentic_products` | `POST /demo/reset` | Clears this user's local Factory link/session state, expires the App 1 cookie, then signs out |
| `agentic_products` | `GET /saml/metadata`, `GET /saml/login`, `POST /saml/acs` | Agentic Products SAML SP metadata, login and ACS |
| `agentic_products` | — | The browser goes directly to Factory's SAML endpoints; Agentic Products does not proxy Factory sign-in |
| `factory` | `GET /saml/metadata` | Publishes local SAML SP metadata |
| `factory` | `GET /saml/login`, `POST /saml/acs` | Starts and validates Entra SAML sign-in |
| `factory` | `/auth/login`, `/auth/callback`, `/auth/logout` | Factory's separate OAuth authorization-code browser sign-in |
| `factory` | `GET /oauth/status` | Compares Factory's OAuth browser identity with the OBO user |
| `factory` | `GET /saml/status` | Matches validated SAML identity to the OBO user |
| `factory` | `GET /identity/validate` | Validates OBO Token B and returns its demo claims |
| `factory` | `POST /github/connect/start` | Validates Token B and creates one-use GitHub OAuth state |
| `factory` | `GET /github/callback` | Exchanges GitHub authorization code and stores encrypted credentials |
| `factory` | `GET /github/status` | Returns linked GitHub login for the current Entra user |
| `factory` | `GET /github/repos` | Refreshes token if needed and lists repositories |
| `factory` | `POST /demo/reset` | Deletes this user's stored GitHub link and local SAML/OAuth session state |
| `factory` | `GET /demo/logout` | Clears Factory's local OAuth and SAML browser cookies after a reset |
| Both | `/health`, `/docs` | Health check and local OpenAPI UI |

## Identity validation in the demo

The member journey starts with Agentic Products SAML. Agentic Products then obtains an OAuth access token for its own API as Token A, silently reusing Entra's browser session when possible. Token A is a separate OAuth credential: a SAML assertion cannot be used as the OBO input. The dashboard automatically exchanges Token A for Token B and calls Factory's `GET /identity/validate` before enabling **Connect GitHub**. Factory validates Token B's signature, issuer, audience, tenant, delegated scope, and user object ID; Agentic Products confirms the returned `tid` and `oid` match the signed-in user. [Microsoft OBO flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow)

The Agentic Products card shows two sets of `aud`, `tid`, and `oid` because it is displaying two credentials. SAML `aud` is the service provider Entity ID; Token A `aud` is the Agentic Products API. The `tid` (tenant) and `oid` (user object ID) repeat because both credentials should identify the same Entra user. Token B has a third `aud`, the Factory API, while keeping that same `tid` and `oid`.

Factory's local SAML service provider is a separate optional demonstration. It does not participate in the member OBO-to-GitHub path; Factory validates that path through its protected API and Token B. Entra SAML assertions carry tenant and object IDs as attributes using Microsoft claim URIs; SAML audience is an AudienceRestriction value, not a JWT `aud` field. [Microsoft SAML token claims](https://learn.microsoft.com/en-us/entra/identity-platform/reference-saml-tokens)

Factory's hosted product SAML integration uses the WorkOS ACS and Entity ID from the supplied guide. The local Python Factory SP uses `http://localhost:8002/saml/acs` instead. The hosted WorkOS ACS must never be replaced with the localhost URL, and the local POC ACS must never be replaced with WorkOS.

## Sequence view and reset

The dashboard presents an interactive sequence diagram with separate lifelines for the Agentic Products Entra registration, Agentic Products API, Factory Entra registration, Factory API, and GitHub. Select a numbered arrow to see the request, response, and a redacted cURL/protocol example. Completed exchanges are green; pending ones are grey. The phases are SAML/OBO validation, GitHub connection, and repository listing. The GitHub button remains disabled until Factory validates Token B for the same `tid` and `oid`. Repository listing resolves the current user's installations of this GitHub App and enumerates only the repositories granted to those installations; it does not call `/user/repos`. This POC stores a GitHub user token (`ghu_`), not a renewable `ghs_` installation token. Values that could identify a session or authorize access are redacted.

**Reset demo & sign out** asks Factory's API to remove the current Entra user's database link (including its encrypted GitHub token), then the browser goes directly to Factory's SAML logout and back to Agentic Products to clear its own session and sign out through Entra. The reset does not revoke GitHub's external app authorization; remove that separately in GitHub Settings if desired. SAML sessions and pending request state live in memory and disappear when their respective service restarts: Agentic Products on port 8001 and Factory on port 8002.
