# Agentic Products + Factory OBO proof of concept

Two local Python services demonstrate the member path from the Agentic Platform 2.0 diagram, with an Agentic Products SAML sign-in followed by OAuth/OBO API validation:

- `agentic_products` signs the browser in with Entra SAML, then obtains OAuth Token A for its API and performs OBO to `factory`.
- `factory` is an OBO-protected API and owns the GitHub callback. It validates Token B before GitHub linking, then encrypts the user's GitHub token against their Entra `tid` and `oid` and calls GitHub's repository API. Factory's local SAML service provider is an optional independent demo.
- A terminal client uses device-code sign-in to get the same App 1 token and call the same API.

## Start here

1. Read [the architecture](docs/architecture.md) and [configuration guide](docs/configuration.md).
   The API contracts are available as [Agentic Products OpenAPI](docs/openapi-agentic-products.yaml) and [Factory OpenAPI](docs/openapi-factory.yaml). They use OpenAPI 3.0.3 for compatibility with the VS Code `openapi-designer` extension. Open either YAML file, then run **OpenApi Designer: Preview** from the Command Palette. When each service is running, FastAPI also serves interactive Swagger UI at `http://localhost:8001/docs` and `http://localhost:8002/docs`.
2. Follow [local setup](docs/setup.md) to configure the two Entra app registrations, the two local SAML Enterprise applications, and GitHub App.
3. If a corporate administrator must create or configure the Entra objects, send them [the Entra administrator handoff](docs/entra-admin-handoff.md). It includes the App 1/App 2 OAuth/OBO settings, SAML Enterprise application settings, claims, callback categories, and the values to return.
4. Read [CLI access and account setup](docs/access.md) for interactive sign-in and which setup steps require you.

## Automated account setup

After `uv lock && uv sync --python 3.13` and `az login --tenant 662f928d-ee5c-4b0a-a0f7-0072c9c0d1bd`, run:

```sh
uv run python scripts/setup_all.py
```

The Azure step creates or updates the registrations and writes local settings to `.env`. The GitHub step opens a preconfigured GitHub App Manifest flow in your browser; you approve creation and installation. Use `--grant-admin-consent` only if you want the script to request tenant-wide consent and your account has the required admin role:

```sh
uv run python scripts/setup_all.py --grant-admin-consent
```

Details, prerequisites, and recovery steps are in [the setup scripts guide](docs/automated-setup.md). Secrets and the GitHub private key stay in git-ignored local files.

## Publish this POC to your GitHub account

After authenticating GitHub CLI in your terminal with `gh auth login`, publish a new private repository with:

```sh
bash scripts/publish_to_github.sh
```

The script uses the authenticated GitHub account, defaults to the repository name `entra-obo-github-poc`, and refuses to publish local environment files, keys, or token databases. To choose another repository name, pass it as the first argument. Review the staged file list before confirming any GitHub CLI prompts.

This is a localhost POC, not a production deployment. Secrets stay in `.env`; GitHub credentials are encrypted in a local SQLite database. Do not paste credentials or tokens into chat.
