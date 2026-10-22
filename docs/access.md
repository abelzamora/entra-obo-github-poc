# CLI access and what the assistant can configure

## Installed CLI inventory

MacPorts reports:

- `uv` 0.12.3 installed
- Python 3.13.15 installed (`python313`); system `python3` still points to Python 3.9.6, so use `uv run`
- Azure CLI 2.89.1 installed
- GitHub CLI (`gh`) not installed; MacPorts has a `gh` port available

For this POC, `gh` is optional. The project setup script uses GitHub's App Manifest flow to preconfigure and register the GitHub App after you approve the creation in your browser. `gh` itself does not provide an app-registration command. Do not provide a GitHub password, PAT, OAuth token, client secret, or private key in chat.

The MacPorts package database warns that its port definitions are more than two weeks old. No packages were installed or upgraded during this check.

## Azure sign-in

Azure CLI is already installed. To let the assistant invoke the CLI in this workspace, use an isolated, temporary Azure CLI config directory and complete device-code sign-in yourself:

```sh
mkdir -m 700 -p /private/tmp/entraid-azure-cli
AZURE_CONFIG_DIR=/private/tmp/entraid-azure-cli az login \
  --tenant 662f928d-ee5c-4b0a-a0f7-0072c9c0d1bd \
  --allow-no-subscriptions \
  --use-device-code
```

The command prints a Microsoft device-login URL and a short code. Open the URL, sign in with your personal Microsoft account, and satisfy its MFA. The CLI caches credentials in the temporary directory; they are not written into this project. After that, the assistant can check the active tenant and use Azure CLI commands from the same workspace without receiving your password or a pasted token.

The managed assistant shell cannot write to the normal `~/.azure` CLI cache and its Azure CLI cannot resolve Microsoft's login endpoint. Run `az login` and `scripts/setup_all.py` in your normal Mac terminal, where your Azure CLI session and browser are available. Package resolution for the new local SAML dependency also needs network access; run `uv lock && uv sync --python 3.13` in that terminal before starting the demo.

The logged-in account still needs the Entra directory role/consent rights for each operation. App registration may be allowed while delegated permission consent or service-principal creation is blocked. If consent requires an administrator, have a tenant admin grant only the requested delegated `access_as_user` permissions; do not grant broad Graph application permissions for this POC.

To remove the CLI's temporary cached login when configuration is complete:

```sh
rm -rf /private/tmp/entraid-azure-cli
```

## GitHub account setup

Run `uv run python scripts/setup_github.py` to start GitHub's supported App Manifest flow. You review the prefilled settings and click **Create GitHub App** while signed into your personal GitHub account. The script exchanges GitHub's one-time callback code and saves credentials locally; it does not print the returned secrets. This still requires you to approve app creation and installation in GitHub. For organization repositories, an organization owner must install/approve the app there.

If you also want `gh` for routine GitHub CLI operations, MacPorts lists the port but it is not installed. Install it from your own terminal with:

```sh
sudo port install gh
```

Then authenticate locally with `gh auth login --hostname github.com --web`. This CLI login is separate from the GitHub App OAuth link used by the POC, and isn't required to run the demo.

## Scope of delegated access

Do not share any account password, MFA code, access token, client secret, GitHub App private key, or `.env` file. Interactive CLI login is the supported way for you to grant the local CLI a user session. Secrets generated for the applications stay in your local `.env` and the local encrypted SQLite database.
