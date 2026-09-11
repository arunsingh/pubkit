# Security

## Reporting

Please report vulnerabilities privately through GitHub's
[security advisory form](https://github.com/arunsingh/pubkit/security/advisories/new)
rather than a public issue. Expect an acknowledgement within a few days.

## Threat model

pubkit holds credentials for accounts that publish under your name. The design
assumes an attacker who can read files on a developer machine or a CI runner.

**What pubkit does**

- Never accepts a password. Browser platforms use an interactive login that
  *you* perform; pubkit persists only the resulting session state.
- Stores API tokens in the OS keychain via `keyring`, falling back to a
  Fernet-encrypted vault whose key also lives in the keychain.
- Encrypts persisted browser sessions at rest, `0600`.
- Redacts secrets from logs with a logging filter rather than by convention.
- Scopes each adapter's `Context` to its own platform's credentials.
- Requires `--confirm` against a hash-pinned plan for any public write.

**What pubkit does not defend against**

- An attacker who already has code execution as your user. A session cookie in
  the keychain is as reachable as your browser's own cookie jar.
- A malicious third-party adapter. Adapters are ordinary Python loaded through
  an entry point; installing one is trusting its author, exactly as with any
  dependency.
- Platform-side compromise.

## In CI

Set `PUBKIT_VAULT_KEY` from your secret store; there is no OS keychain on a
runner. Gate the publish job behind a GitHub environment with a required
reviewer — `--confirm` is a guard rail, not an authorisation system.

Never commit `.pubkit/`. The scaffold's `.gitignore` excludes it.
