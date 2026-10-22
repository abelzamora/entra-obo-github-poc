# Microsoft Entra administrator handoff

Use this checklist to request the corporate Entra configuration for the Agentic Products → Factory member flow. It is written for an administrator who can create and inspect app registrations and Enterprise applications. You do not need Entra read access to prepare this request; ask the administrator to return the identifiers and federation values listed at the end.

> **Important distinction:** an OAuth **app registration** defines a client/API, OAuth redirect URIs, credentials, and delegated scopes. A SAML **Enterprise application** defines the SAML service-provider trust: Entity ID, ACS (Reply URL), claims, signing, assignment, and IdP metadata. SAML ACS URLs do not go in the OAuth redirect URI list. The same business application can need both configurations, but they are separate Entra settings/objects. Microsoft's SAML setup guide and OAuth redirect URI guide describe these as different configuration paths ([SAML Enterprise app setup](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/add-application-portal-setup-sso), [OAuth redirect URI setup](https://learn.microsoft.com/en-us/entra/identity-platform/how-to-add-redirect-uri)).

## Architecture and objects to create

The member flow uses **two app registrations**:

1. **Agentic Products** is the browser-facing web client and the API/resource that issues Token A. It calls the Factory API using OBO.
2. **Factory** is the protected API/resource that issues Token B's audience and validates that token before handling GitHub requests.

For browser SAML sign-in, configure a SAML Enterprise application for each SP that needs SAML. For this POC, the local Agentic Products SP and optional local Factory SP each have their own non-gallery SAML Enterprise application. If users also need SAML into the hosted Factory product, that is a separate Enterprise application configured with the Entity ID and ACS supplied by Factory (in the shared guide, a WorkOS ACS). Do not substitute the local ACS for the hosted Factory ACS.

The API delegation is OAuth/OBO, even when the browser sign-in starts with SAML. The SAML assertion establishes the Agentic Products browser session; it is **not** the OBO assertion. Agentic Products obtains OAuth Token A for its own API and presents Token A to Entra's OBO token endpoint to get Token B for Factory. OBO passes delegated user identity through the API chain and does not use a redirect URI ([Microsoft OBO flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow)).

## A. App registration: Agentic Products (App 1)

Create an app registration named `Agentic Products` in the corporate tenant. Choose **Accounts in this organizational directory only** (single tenant/workforce tenant).

### Authentication platform

Under **Manage → Authentication → Add a platform → Web**, add the actual HTTPS OAuth callback for the deployed Agentic Products server:

```text
https://<agentic-products-host>/<oauth-callback-path>
```

For the local POC only, the callback is:

```text
http://localhost:8001/auth/callback
```

The callback must be supplied by the Agentic Products deployment owner and must match exactly. Keep authorization-code flow with PKCE. Do not enable implicit access-token or ID-token issuance for this server-side flow. The local terminal device-code demo separately requires **Allow public client flows = Yes**; this is not needed for the production web client unless the terminal client will also be supported. Redirect URI configuration is documented by Microsoft ([Web redirect URI configuration](https://learn.microsoft.com/en-us/entra/identity-platform/how-to-add-redirect-uri)).

### Expose Agentic Products API

Under **Manage → Expose an API**:

| Setting | Requested value |
| --- | --- |
| Application ID URI | `api://<APP1_CLIENT_ID>` (or an approved stable URI) |
| Delegated scope | `access_as_user` |
| Who can consent | Admins only, if corporate policy requires admin approval; otherwise follow tenant policy |
| Admin consent display name | `Access Agentic Products as the signed-in user` |
| Admin consent description | `Allow Agentic Products to call its API as the signed-in user and perform a downstream OBO exchange to Factory` |
| User consent display name/description | Set only if user consent is allowed by corporate policy |
| State | Enabled |

The full scope used by the local POC is `api://<APP1_CLIENT_ID>/access_as_user`. Configure the access-token version as **2** in the app manifest (`api.requestedAccessTokenVersion = 2`) for this API.

### Credentials and permissions

- Create a confidential-client credential for Agentic Products. For the POC this is a client secret; the production team should use the organization's approved certificate or workload-identity approach where applicable.
- Deliver the secret only through the corporate secret manager or another approved secure channel. Do not put it in email, a ticket, chat, or source control.
- Under **API permissions**, add the **Factory** API's delegated `access_as_user` permission after App 2 exists. Grant tenant admin consent if the tenant requires it.
- If the terminal device-code demo is required, enable public client flows. It is optional for the browser OBO path.

## B. App registration: Factory (App 2 / downstream API)

Create an app registration named `Factory` in the same tenant, also single-tenant.

### Expose the Factory API

Under **Manage → Expose an API**:

| Setting | Requested value |
| --- | --- |
| Application ID URI | `api://<APP2_CLIENT_ID>` (or an approved stable URI) |
| Delegated scope | `access_as_user` |
| Who can consent | Admins only if required by policy; otherwise follow tenant policy |
| Admin consent display name | `Access Factory as the signed-in user` |
| Admin consent description | `Allow Agentic Products to call Factory APIs on behalf of the signed-in user` |
| State | Enabled |

Set `api.requestedAccessTokenVersion = 2` in the App 2 manifest. The full delegated permission is `api://<APP2_CLIENT_ID>/access_as_user`.

### Redirect URI and credential

The Factory API does **not** need an OAuth redirect URI or client secret for OBO itself. OBO is a server-to-server token exchange between Agentic Products and Entra. The local POC includes an optional, separate Factory interactive OAuth sign-in page; if that diagnostic flow is being used, add this Web redirect URI and credential:

```text
http://localhost:8002/auth/callback
```

For a deployed Factory OAuth diagnostic, use the Factory deployment's exact HTTPS callback. Do not add the SAML ACS or GitHub callback as Entra OAuth redirect URIs. Any Factory API credential used only by that optional interactive client must be kept separate from the App 1 OBO credential.

### Optional pre-authorized client

If the tenant's consent policy requires explicit pre-authorization, in App 2 **Expose an API → Authorized client applications**, add the Agentic Products App 1 client ID and select the `access_as_user` scope. This is an administrator decision; it does not replace App 1's API permission and tenant consent grant.

## C. Connect the apps for OBO

After both registrations exist:

1. In **Agentic Products → API permissions → Add a permission → My APIs → Factory**, select **Delegated permissions → `access_as_user`**.
2. Confirm the permission appears as `api://<APP2_CLIENT_ID>/access_as_user`.
3. Select **Grant admin consent for <corporate tenant>** if user consent is not allowed or policy requires admin approval.
4. Verify the permission status shows consent granted.
5. Confirm the App 1 confidential-client credential is active and available to the service through the approved secret store.

OBO must use a **delegated** permission. Do not configure an application permission for this member flow. The API validates the access token's issuer, signature, audience, delegated scope, tenant ID (`tid`), and user object ID (`oid`). The downstream Token B should have the Factory API as `aud` and preserve the same user `tid` and `oid` as Token A ([Microsoft OBO protocol](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow)).

## D. SAML Enterprise application: Agentic Products SP

Create a non-gallery Enterprise application for the Agentic Products SAML service provider, or configure the existing assigned SAML Enterprise application if the administrator confirms it is the correct object. In **Enterprise applications → [app] → Single sign-on → SAML → Basic SAML Configuration**, use the values supplied by the Agentic Products deployment owner.

For this local POC, the values are:

| Basic SAML field | Local POC value |
| --- | --- |
| Identifier (Entity ID) | `http://localhost:8001/saml/metadata` |
| Reply URL (ACS URL) | `http://localhost:8001/saml/acs` |
| Sign-on URL | `http://localhost:8001/saml/login` (optional; omit if the portal rejects localhost here) |
| Logout URL | Leave blank unless the SP owner supplies a supported SAML logout endpoint |

For corporate use, replace every localhost URL with the exact HTTPS Entity ID, ACS, and (if supported) sign-on URL supplied by the Agentic Products service owner. Do not invent a URL. The Entity ID identifies the SP; the ACS is where Entra posts the SAML response. Microsoft's SAML setup guide describes these fields as separate from app-registration OAuth redirects ([SAML SSO setup](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/add-application-portal-setup-sso)).

### Claims and signing

On **Attributes & Claims**, configure the fields the SP requires:

| Claim purpose | Claim URI / source | Required by this POC |
| --- | --- | --- |
| Name ID | `user.userprincipalname` (email-address format is acceptable to this POC) | Yes, for a stable sign-in name |
| Tenant ID | `http://schemas.microsoft.com/identity/claims/tenantid` | Yes |
| User object ID | `http://schemas.microsoft.com/identity/claims/objectidentifier` | Yes |
| Email | `user.mail` | Useful for display |
| Given name | `user.givenname` | Optional |
| Surname | `user.surname` | Optional |

Configure the signing option so the **SAML assertion is signed**. The local SP validates the signed assertion; an unsigned outer Response is accepted when the assertion itself is signed. In **SAML Certificates**, ensure a signing certificate is active. Copy the **App Federation Metadata URL from this Agentic Products Enterprise application**; the application uses it to load the signing keys. Do not use tenant-wide federation metadata or the Factory enterprise application's metadata URL. Assign the test users/groups under **Users and groups** and confirm assignment is required/appropriate for the app.

## E. SAML Enterprise application: Factory

There can be two distinct Factory SAML trusts; the administrator should configure only the one(s) required by the target:

### Local Factory SAML POC (optional diagnostic)

This is a second non-gallery Enterprise application for the local Python Factory SP, not the App 2 registration and not the hosted product trust.

| Basic SAML field | Local POC value |
| --- | --- |
| Identifier (Entity ID) | `http://localhost:8002/saml/metadata` |
| Reply URL (ACS URL) | `http://localhost:8002/saml/acs` |
| Sign-on URL | `http://localhost:8002/saml/login` (optional; omit if the portal rejects localhost here) |

Add the same required Name ID, tenant ID, and object ID claims; ensure the assertion is signed; assign test users/groups; and return the **App Federation Metadata URL for this Factory SAML Enterprise application**. This separate sign-in is not part of the Agentic Products → Factory OBO member path.

### Hosted Factory product SAML (only if required by Factory)

Create/configure the separate Enterprise application required by the Factory product's SAML guide. Copy the **Entity ID, ACS URL, optional sign-on URL, claims, and signing requirements from the Factory organization setup wizard**. In the screenshots shared for this POC, the ACS uses a connection-specific `https://auth.workos.com/sso/saml/acs/<connection-id>` URL. The administrator must use the exact values shown in the current Factory tenant/wizard; do not reuse either localhost ACS. Return the active certificate details or metadata values that Factory's wizard requests.

## F. Verify and return these values to the requester

Please return the following through an approved channel (IDs and metadata URLs are not credentials; secrets are):

| Value to return | Where the admin finds it |
| --- | --- |
| Corporate tenant ID (`tid`) and tenant name | Entra tenant overview |
| App 1 Application (client) ID | Agentic Products app registration → Overview |
| App 1 Application ID URI and full scope | App 1 → Expose an API |
| App 2 Application (client) ID | Factory app registration → Overview |
| App 2 Application ID URI and full scope | App 2 → Expose an API |
| Confirmation App 1 has delegated `access_as_user` permission to App 2 | App 1 → API permissions |
| Confirmation admin consent was granted, if needed | App 1 → API permissions status |
| OAuth Web redirect URIs configured for each app | Each app registration → Authentication |
| Entity ID, ACS, and sign-on URL configured for each SAML SP | Each Enterprise application → Single sign-on → SAML |
| App Federation Metadata URL for each SAML Enterprise application | Its SAML page → SAML Signing Certificate |
| Active SAML signing certificate thumbprint/expiry and assertion-signed setting | Its SAML page → SAML Signing Certificate |
| Confirmation required claims and test-user/group assignment are configured | Its SAML Attributes & Claims / Users and groups pages |
| App 1 secret/certificate access method and expiry | Provide only through approved secret management; never send in this document |

If the requester cannot read the tenant, include screenshots or an exported configuration summary showing these values and the final consent status. Do not send client secrets, certificates' private keys, access tokens, or refresh tokens in screenshots or email.

## G. Production handoff: replace local-only values

The POC uses HTTP localhost values for local callbacks. Before deployment, the service owners must provide stable HTTPS URLs and request the administrator to register those exact values. Keep these categories separate:

| Flow | Example / field | Configured on |
| --- | --- | --- |
| Agentic Products OAuth | `<APP1_HTTPS_HOST>/auth/callback` | App 1 registration → Authentication → Web redirect URI |
| Agentic Products SAML | `<APP1_ENTITY_ID>`, `<APP1_HTTPS_HOST>/saml/acs` | Agentic Products SAML Enterprise app → Basic SAML Configuration |
| Factory diagnostic OAuth (optional) | `<FACTORY_HTTPS_HOST>/auth/callback` | App 2 registration → Authentication → Web redirect URI |
| Factory OBO API scope | `api://<APP2_CLIENT_ID>/access_as_user` | App 2 → Expose an API; requested by App 1 |
| Factory SAML | Product-provided Entity ID and ACS | Factory SAML Enterprise app → Basic SAML Configuration |
| GitHub OAuth callback | `<FACTORY_HTTPS_HOST>/github/callback` | GitHub App settings, **not** Entra |

Use separate development and production registrations if the organization requires isolation. Remove localhost callbacks from production registrations when no longer needed. The app's redirect URI must exactly match the runtime URI; Microsoft rejects unregistered callback URLs.

## H. This POC's Entra settings (personal tenant reference)

The local project has been configured against personal tenant `662f928d-ee5c-4b0a-a0f7-0072c9c0d1bd`. The app IDs and secrets are kept in the ignored local `.env` file and are intentionally not copied into this repository. **Do not copy the personal tenant ID, personal app registrations, secrets, or localhost-only enterprise apps into the corporate tenant.** Use section G with the corporate app/environment owners' actual HTTPS URLs.

The repository's implementation detail and route list are in [the architecture](architecture.md); local configuration and run instructions are in [the configuration guide](configuration.md) and [setup guide](setup.md).
