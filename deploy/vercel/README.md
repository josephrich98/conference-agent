# Vercel proxy for conference-agent (RETIRED — do not deploy)

> **Status: not in use.** The public site is now the static bundle served
> directly from Vercel (`https://conferenceagent.vercel.app`, deployed from
> `dist/` — see the repo `CLAUDE.md` and `DEPLOY.md`). This proxy is kept only
> as a record of the original AWS-fronted topology.
>
> **Do not run `vercel --prod` here.** This directory is intentionally *unlinked*
> from any Vercel project (its local `.vercel/` link has been removed and
> `.vercel` is gitignored). Both this proxy and `dist/` used to link to the same
> `conferenceagent` project, so deploying from here would overwrite the public
> static site with a rewrite to AWS. If you ever deploy it again, link it to a
> **separate** project first (`vercel link --project conferenceagent-aws`), never
> to `conferenceagent`.
>
> **The AWS stack is demoed directly, not through this proxy.** When deployed,
> the SAM stack is reachable at its own (unadvertised) generated CloudFront URL,
> which coexists with the public Vercel site on a separate URL and needs no
> Vercel layer at all. The stack is normally torn down to save cost; bring-up,
> teardown, and the current status live in the gitignored `DEPLOY_AWS.md` at the
> repo root.

A zero-build Vercel project whose only job is to give the service a memorable,
free URL (`https://conferenceagent.vercel.app`) that forwards every request to
the AWS stack. Vercel terminates TLS for `*.vercel.app` and proxies server-side,
so the browser only ever sees the `*.vercel.app` hostname.

The app still runs entirely on AWS — this is a thin rewrite layer, nothing more.
The proxy target is the **CloudFront** distribution (not the Lambda Function URL
directly): the Function URL is locked to `AuthType: AWS_IAM` and only CloudFront,
via Origin Access Control, can invoke it. Vercel cannot produce the SigV4
signature the Function URL requires, but CloudFront is public, so Vercel proxies
to CloudFront and CloudFront signs the origin request.

```
Browser → conferenceagent.vercel.app (Vercel)
        → dvkzefjrppdt8.cloudfront.net (CloudFront, OAC)
        → *.lambda-url.us-east-1.on.aws (Lambda Function URL, IAM)
        → FastAPI
```

To repoint at a different CloudFront distribution (or a custom domain later),
edit `destination` in `vercel.json` and redeploy.

## Deploy (only if reviving the AWS-fronted topology)

This proxy must **never** be deployed to the `conferenceagent` project — that
project is the public static site (served from `dist/`), and deploying this
rewrite there would replace the live site with a proxy to AWS. Deploy only to a
**separate** project, and only via `deploy.sh`, which hard-aborts if the linked
project is `conferenceagent`:

```bash
cd deploy/vercel
npx vercel login                                      # one-time, interactive (OAuth in your browser)
npx vercel link --yes --project conferenceagent-aws   # a SEPARATE project — never "conferenceagent"
./deploy.sh                                            # guarded deploy; prints the production URL
```

A separate project (e.g. `conferenceagent-aws`) serves the proxy at its own
`*.vercel.app` subdomain, leaving the public `https://conferenceagent.vercel.app`
static site untouched. The guard in `deploy.sh` is the safety net; since the AWS
stack is normally torn down, this path is exercised only when deliberately
bringing AWS back up.

## Note on the custom-domain path

A real custom domain (e.g. `conferences.example.com`) can attach to *either*
layer: to Vercel (Project → Settings → Domains — simplest, free aside from
registration) or to CloudFront (set `DomainName` + `AcmCertificateArn` in
`infra/template.yaml` — see `DEPLOY.md`). The AWS path is the one that keeps the
domain wholly within the AWS stack.
