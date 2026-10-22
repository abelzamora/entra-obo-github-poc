# Local setup and runbook

## Automated account setup

For repeatable setup from the command line, use [the automated setup guide](automated-setup.md). It creates or updates both Entra registrations and starts GitHub's browser-based manifest flow. The manual reference steps below describe the resulting configuration.

If a corporate administrator must create the Entra objects for you, send them the [Entra administrator handoff](entra-admin-handoff.md). It includes the settings and asks for the identifiers, consent status, and SAML metadata you will need because you cannot inspect the tenant yourself.

## 1. Create Entra applications

Use the settings listed in [configuration](configuration.md). Create two single-tenant registrations in tenant `662f928d-ee5c-4b0a-a0f7-0072c9c0d1bd`:

1. **Agentic Products app registration**: Web redirect `http://localhost:8001/auth/callback`; expose `api://<App1 client ID>/access_as_user`; enable public client flow; create a client secret; request its own delegated scope and the Factory API scope.
2. **Factory app registration**: configure Web redirect `http://localhost:8002/auth/callback`, expose `api://<App2 client ID>/access_as_user`, and create a client secret for Factory's interactive OAuth sign-in. The same registration remains the OBO API resource; OBO itself does not use that redirect URI.
3. Add Factory API's delegated scope to Agentic Products API permissions and grant consent.
4. **Local Agentic Products SAML Enterprise application**: use Entity ID `http://localhost:8001/saml/metadata`, Reply URL `http://localhost:8001/saml/acs`, and Sign-on URL `http://localhost:8001/saml/login`.
5. **Local Factory SAML Enterprise application**: follow [Local POC Factory SAML values](configuration.md#local-poc-factory-saml-values), separate from the Factory OBO API registration and hosted Factory SSO integration.
6. For tokens from the v2 endpoint, set `api.requestedAccessTokenVersion` to `2` on both app manifests if needed.

For both local SAML Enterprise applications, configure the Basic SAML values and assign your test user. Set `APP1_SAML_IDP_METADATA_URL` to Agentic Products' **App Federation Metadata Url**, and `SAML_IDP_METADATA_URL` to Factory's own **App Federation Metadata Url**. These are app-specific because the signing certificate belongs to each Enterprise application; do not use the tenant-wide metadata URL for signature validation.

The Factory app registration now has an OAuth redirect URI for its own authorization-code sign-in and also exposes the API scope used by OBO. SAML Reply URLs belong on their corresponding Enterprise applications, not in the OAuth redirect URI list. For the hosted Factory product, use the WorkOS ACS and Entity ID shown in its wizard on that integration's separate Enterprise application.

When registering, choose single-tenant/workforce accounts. Your personal Microsoft account must be a user or guest in that tenant. If Entra blocks the self-scope setup for App 1 in the portal, stop and report the exact portal error; don't create a third registration yet, as we want to keep the initial POC aligned to the supplied two-app flow.

## 2. Create the GitHub App

Run `uv run python scripts/setup_github.py` or follow the GitHub settings table in [configuration](configuration.md). Install it on your personal account, choose **Only select repositories**, and select the repositories you want Factory to list. The OAuth callback must exactly match `http://localhost:8002/github/callback`.

For repositories owned by an organization, that organization needs to install/approve the GitHub App too. Your personal GitHub settings cannot grant it access to a work organization.

## 3. Configure local secrets

```sh
cp .env.example .env
```

Fill in the IDs and secret values locally. Generate the token encryption key after dependencies install:

```sh
uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Paste the result into `TOKEN_ENCRYPTION_KEY` in `.env`. Do not send the `.env` file, secret values, or the generated key in chat. Losing this key means deleting `local_tokens.sqlite3` and reconnecting GitHub.

## 4. Install Python dependencies

`uv` and Python 3.13 are already available through MacPorts. Sync the project environment:

```sh
uv lock
uv sync --python 3.13
```

`python3-saml` and its XML signature dependencies are part of the project dependency set. Resolve the lock file with `uv lock` before syncing if dependencies changed. On macOS with Python 3.13, the package publishes an arm64 wheel; if installation falls back to a source build, install the matching MacPorts XML security libraries first.

If your shell resolves system Python 3.9 when running Python directly, that's okay; use `uv run` for every project command.

If package resolution reports DNS/network errors, run the command from a network-enabled terminal. The managed execution environment used while preparing this project currently cannot reach PyPI.

## 5. Run both services

Open two terminal windows in the project directory.

Terminal A:

```sh
uv run uvicorn factory:app --host 127.0.0.1 --port 8002 --reload
```

Terminal B:

```sh
uv run uvicorn agentic_products:app --host 127.0.0.1 --port 8001 --reload
```

Open <http://localhost:8001> and sign in to Agentic Products with SAML. After the callback, Agentic Products obtains Token A through OAuth and automatically calls Factory with an OBO Token B to validate the same `tid` and `oid`. Wait for the Factory API status to turn green, then click **Connect GitHub**. Factory owns the GitHub callback and stores the encrypted token against that Entra user. Click **List my repositories** to exercise the API. Factory's own SAML sign-in remains a separate optional flow and is not part of this OBO member journey. MFA, if prompted, is determined by your account and tenant policies.

The interactive sequence diagram shows the Entra registrations, both APIs, and GitHub as lifelines. Select an arrow to inspect its request, response, and redacted cURL/protocol example. Completed exchanges are green; pending exchanges are grey. **Clear data and sign out** clears the local SAML sessions and stored GitHub token, then signs out of App 1 and Entra. It does not revoke GitHub's external app authorization, which can be removed in GitHub Settings.

Use `http://localhost:8001` throughout the browser sign-in. Do not mix it with `http://127.0.0.1:8001`; the callback cookie is host-bound and a host change causes the authorization-state check to fail.

Health/API pages:

- <http://localhost:8001/health>
- <http://localhost:8002/health>
- <http://localhost:8001/docs>
- <http://localhost:8002/docs>

## 6. Try the terminal client

```sh
uv run python terminal_demo.py connect --open-browser
uv run python terminal_demo.py repos
uv run python terminal_demo.py status
```

The terminal client authenticates with Entra device code, obtains Token A for Agentic Products, and calls Agentic Products. Factory and GitHub credentials stay on the server side.

## 7. Stop and reset

Stop each server with `Ctrl-C`. During the demo, use **Reset demo & sign out** to clear the current user's local Factory link/session and sign out. You can also delete `local_tokens.sqlite3` to clear all local GitHub links. Neither action revokes GitHub's external app authorization; revoke that under GitHub **Settings → Applications → Authorized GitHub Apps**.
