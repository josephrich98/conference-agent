# Security

## Reporting a vulnerability

Please report security problems privately through GitHub's
[vulnerability reporting](https://github.com/josephrich98/conference-agent/security/advisories/new),
not in a public issue. Include what you found, how to reproduce it, and what
an attacker could do with it.

In scope: the live site (`https://conferenceagent.vercel.app`), its functions
under `/api/`, the `conference-agent` package, and this repository's scripts.

## What the deployment holds

The site is static files plus seven Vercel Functions
(`conference_agent/web/vercel/api/`). The only personal data is subscriber
email addresses (private Vercel Blob store), keyed hashes used for rate limits,
and the optional names in form submissions, which are public pull requests.
The [privacy page](https://conferenceagent.vercel.app/privacy/) describes each
of these.

## Credentials

| Secret | Where it lives | What it grants |
|---|---|---|
| `SUBSCRIBE_SECRET` | Vercel (Production) and `~/.config/conference-agent/secrets.env` | Signs confirm / unsubscribe links and the `ca_verified` cookie |
| `SUBSCRIPTIONS_TOKEN` | Vercel (Production) and the local secrets file | Reads the full subscriber list (`/api/subscriptions`) |
| `SMTP_USER` / `SMTP_PASSWORD` | Vercel (Production) and the local secrets file | Sends mail as the site's Gmail account (an app password) |
| `GITHUB_TOKEN` | Vercel (Production) | Fine-grained token: Contents and Pull requests on this repository |
| `BLOB_READ_WRITE_TOKEN` | Vercel (set by the Blob integration) | Reads and writes the subscription store |
| Vercel CLI login / `VERCEL_TOKEN` | Maintainer machine | Deploys the site |
| `ANTHROPIC_API_KEY` | Only for the `api` discovery backend | Metered API use |

No secret is committed. The pre-commit hook (`.githooks/`) and CI run gitleaks,
and GitHub secret scanning with push protection is enabled.

## Incident runbook

When a credential may have leaked (committed, logged, or the machine holding it
was compromised):

1. **Revoke and replace it first**, then investigate.
   - `SUBSCRIPTIONS_TOKEN`: generate a new value (`openssl rand -hex 32`), set it
     in Vercel and in the local secrets file, and redeploy. Treat the subscriber
     list as disclosed (step 3).
   - `SUBSCRIBE_SECRET`: set the new value as `SUBSCRIBE_SECRET` and move the old
     one to `SUBSCRIBE_SECRET_PREVIOUS` in Vercel, update the local file, and
     redeploy. Unsubscribe links already emailed keep working. Remove
     `SUBSCRIBE_SECRET_PREVIOUS` once you no longer need those links to work,
     because anyone holding the old key can forge links until then.
   - `SMTP_PASSWORD`: revoke the Gmail app password at
     <https://myaccount.google.com/apppasswords>, create a new one, update Vercel
     and the local file.
   - `GITHUB_TOKEN`: revoke it under GitHub → Settings → Developer settings →
     Fine-grained tokens, create a replacement with the same two permissions on
     this repository only, and update Vercel.
   - `BLOB_READ_WRITE_TOKEN`: rotate it from the Blob store's settings in the
     Vercel dashboard.
   - Vercel token: revoke it under Vercel → Account Settings → Tokens.
2. **Look for misuse.** Vercel → Logs for the `/api/*` functions; the Blob
   store's contents; the repository's branches and pull requests (the GitHub
   token can create both); Gmail's Sent folder.
3. **If subscriber addresses were exposed**, email the affected subscribers what
   happened, and record the date, cause, and fix in a GitHub security advisory.
4. **Fix the cause** (how the secret leaked), then note it in the advisory.

For a site defacement or a bad deploy, roll back with
`npx vercel rollback` from `dist/` (or promote a previous deployment in the
Vercel dashboard), then fix and redeploy.
