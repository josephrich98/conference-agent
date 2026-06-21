# Vercel proxy for conference-agent

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

## Deploy

```bash
cd deploy/vercel
npx vercel login                                 # one-time, interactive (OAuth in your browser)
npx vercel link --yes --project conferenceagent  # create/link the "conferenceagent" project
npx vercel --prod --yes                          # deploy; prints the production URL
```

The project name determines the subdomain. It is named `conferenceagent`, which
serves the site at `https://conferenceagent.vercel.app`.

## Note on the custom-domain path

A real custom domain (e.g. `conferences.example.com`) can attach to *either*
layer: to Vercel (Project → Settings → Domains — simplest, free aside from
registration) or to CloudFront (set `DomainName` + `AcmCertificateArn` in
`infra/template.yaml` — see `DEPLOY.md`). The AWS path is the one that keeps the
domain wholly within the AWS stack.
