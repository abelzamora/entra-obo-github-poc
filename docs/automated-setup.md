# Automated Entra and GitHub setup

The setup scripts configure the two-app localhost POC. They create/update Entra application registrations through the signed-in Azure CLI and use GitHub's supported GitHub App Manifest flow to create the GitHub App after you approve it in a browser.

## Before running

1. Run `uv sync --python 3.13` from the project directory.
2. Sign the Azure CLI into the intended tenant from your Mac terminal:

   ```sh
   az login --tenant 662f928d-ee5c-4b0a-a0f7-0072c9c0d1bd --allow-no-subscriptions
   az account show --query tenantId --output tsv
   ```

   Confirm the printed tenant ID is `662f928d-ee5c-4b0a-a0f7-0072c9c0d1bd`. A VS Code Azure extension sign-in is separate from Azure CLI sign-in.

3. Close any other app already using the GitHub manifest callback port only if you choose a fixed port. By default the script selects an available local port.

## Run both setup steps

```sh
uv run python scripts/setup_all.py
```

The command is safe to rerun for the same app registrations. It finds registrations by the exact names `agentic_products` and `factory`, configures them, and appends only the intended delegated scopes. It does not delete other permissions or credentials. If `.env` already contains the App 1 secret, the script retains it. If it doesn't, the script creates a new one-year secret and saves it directly to `.env` without printing it. `.env` is written with owner-only file permissions.

The Entra changes are:

- `agentic_products`: single tenant, web redirect URI `http://localhost:8001/auth/callback`, public client flow enabled for the terminal demo, v2 token settings, `api://<client-id>/access_as_user` scope, and a client secret for OBO.
- `factory`: single tenant, web redirect URI `http://localhost:8002/auth/callback`, v2 token settings, `api://<client-id>/access_as_user` scope, and a client secret for its interactive OAuth sign-in.
- `agentic_products` requests its own delegated scope for Token A and `factory`'s delegated scope for the OBO exchange.
- `.env` gets the tenant/app IDs, scope strings, both OAuth callback URLs, App 1 and App 2 secrets, and a local Fernet encryption key.

The script configures Factory as both an OAuth web client and the OBO API registration. It does not create the local Agentic Products or Factory SAML Enterprise applications. Create both local SAML Enterprise applications manually as described in [configuration](configuration.md#entra-app-1-agentic-products) and [configuration](configuration.md#local-poc-factory-saml-values). The hosted Factory SSO Enterprise application is also separate and uses the values in Factory's organization setup wizard.

If you want the script to attempt tenant-wide admin consent after adding the delegated scopes, run:

```sh
uv run python scripts/setup_all.py --grant-admin-consent
```

Azure CLI documents this command as requiring the Global Administrator role. Otherwise, use the portal's **API permissions → Grant admin consent** action if your role permits it, or let a user consent to the delegated scopes at sign-in if tenant policy permits it. No Microsoft Graph permissions are requested.

## GitHub browser step

After the Entra script finishes, the GitHub script starts a loopback-only callback and opens a local setup page. Click **Continue to GitHub**, sign in to the personal GitHub account, review the prefilled app, and click **Create GitHub App**. GitHub sends a short-lived setup code back to the local script, which exchanges it and:

- saves the GitHub App ID, Client ID, and Client Secret in `.env`;
- saves the GitHub-generated private key to `secrets/github-app-private-key.pem` with owner-only permissions; this key is retained for future installation-token work but is not used by the current repository-listing demo;
- opens the app installation page. Install it on your personal account, choose **Only select repositories**, and select the repositories you want Factory to list.

The GitHub App is private to your account, has its localhost callback configured, requests only repository Metadata read permission, and has webhooks disabled. The manifest callback code and credentials are not printed. GitHub App registration is supported through this manifest flow, but GitHub still requires you to approve the app creation and installation in your browser. [GitHub App manifest flow](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest)

To run only one step:

```sh
uv run python scripts/setup_entra.py
uv run python scripts/setup_github.py
```

The GitHub script skips registration if `.env` already has a GitHub Client ID and Client Secret. Pass `--force-new` only when you intentionally want to create a second GitHub App.

## Finish and run the POC

If you skipped tenant-wide consent, grant the required delegated scopes in Entra. Then run the two servers in separate terminals:

```sh
uv run uvicorn factory:app --host 127.0.0.1 --port 8002
uv run uvicorn agentic_products:app --host 127.0.0.1 --port 8001
```

Open <http://localhost:8001>, sign in with Entra, then use the separate Factory OAuth and SAML actions. Factory's OAuth callback is `http://localhost:8002/auth/callback`; its local SAML ACS is `http://localhost:8002/saml/acs`; the GitHub OAuth callback remains `http://localhost:8002/github/callback`.

If the page reports a Factory error, verify `http://localhost:8002/health` returns `{"status":"ok","application":"factory"}` and check the Factory terminal output. App 1 now includes Factory's HTTP status and safe error detail in the page response; raw upstream response bodies are not returned.

## Security and cleanup

- Never commit or share `.env`, `secrets/`, `local_tokens.sqlite3`, the Azure CLI cache, or screenshots containing secret values.
- The scripts do not print Azure or GitHub client secrets or the GitHub private key.
- Azure App 1 secret expires after one year. Rotate it in Entra and update `ENTRA_APP1_CLIENT_SECRET` in `.env` if it expires.
- Remove the local GitHub App under **GitHub Settings → Developer settings → GitHub Apps** when finished; uninstall it from the personal account separately.
- Delete `.env`, `secrets/`, and `local_tokens.sqlite3` to remove local POC credentials. Deleting local files does not revoke the Entra apps or GitHub App; remove those from their account portals as well.
