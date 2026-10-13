# Sign-in providers

How an operator adds Google, any OpenID Connect provider or GitHub as a sign-in option on a hosted server, and how provider accounts are linked.

## Add a provider

1. Register `<public_url>/auth/<name>/callback` as the redirect URI with the provider.
2. Add a `[providers.<name>]` section to `config.toml`:

   ```toml
   [providers.google]
   type = "oidc"                                 # any OpenID Connect issuer: Google, Microsoft, Keycloak, Authentik, ...
   label = "Google"
   issuer = "https://accounts.google.com"        # exactly as the issuer's discovery document says
   client_id = "1234.apps.googleusercontent.com"
   client_secret_env = "GOOGLE_CLIENT_SECRET"    # read from the environment (or client_secret = "..." in the file)

   [providers.github]
   type = "github"
   label = "GitHub"
   client_id = "Iv1.abc"
   client_secret_env = "GITHUB_CLIENT_SECRET"
   ```

3. Put the secrets in the service's environment file (see [Operations](operations.md)) and restart.

## How sign-in is checked

- Sign-in uses the authorization code flow with PKCE, a state bound to the browser and, for OIDC, a nonce.
- The ID token comes straight from the token endpoint over verified TLS. Its issuer, audience, expiry and nonce are checked.
- Only verified email addresses are accepted. For GitHub, that is the verified primary one.
- Two-step sign-in still applies.

## Link a provider to an existing account

- From the account's page, with "Connect".
- Automatically, when the site admin allows it and the existing account's email is confirmed: it signed up through a provider or through an invite sent to that address.

A password account is never taken over by someone who merely owns the same address at a provider.

## Next

- [Install a hosted server](install.md)
- [Operations: TLS, systemd, backups, updates](operations.md)
- [Hosted features](features.md)
