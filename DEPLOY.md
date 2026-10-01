# Deploying conference-agent

```bash
bash scripts/deploy_static.sh
```

That is the whole deploy. The live site, `https://conferenceagent.vercel.app`, is
a static bundle on Vercel: no per-request compute and no database, so traffic
cannot incur cost. An AWS stack is kept as a [legacy alternative](#legacy-aws).

## Static site (the live path)

`deploy_static.sh` runs two steps:

```bash
python scripts/build_static.py                 # snapshot the DB + UI into dist/
( cd dist && npx vercel deploy --prod --yes )  # publish to the conferenceagent project
```

- Preview locally with `( cd dist && python -m http.server 8000 )`.
- Deploy **only from `dist/`**. `deploy/vercel/` is the retired CloudFront proxy,
  linked to the same Vercel project; deploying from it would point the public
  URL back at AWS.
- To refresh the live data, re-run the deploy after a discovery run. There is no
  database to push.
- The bundle is self-contained, so any static host works, e.g.
  `npx wrangler pages deploy dist --project-name conference-agent` for
  Cloudflare Pages.

Search, sorting, CSV export, `.ics` downloads, and AI search all run in the
browser over `dist/data/conferences.json` (see `CLAUDE.md` for how the JS ports
are kept in parity with the Python). The only feature missing from the static
site is the whole-list "Subscribe (.ics)" feed, which needs a server.

---

## Legacy: AWS

> Not the deploy target. A private, on-demand demo of the dynamic backend, kept
> torn down when unused.

```
Browser ─> CloudFront ─(SigV4)─> Lambda Function URL (FastAPI) ─VPC─> RDS PostgreSQL
```

The Function URL is IAM-locked, so all traffic enters through CloudFront. The
code is plain SQLAlchemy; only the connection string differs from local SQLite.

### REST API (FastAPI backend)

Served by this stack or locally with `uvicorn web.app:app` (not by the static
site). Interactive docs are at `/docs`.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/search?q=<query>&format=json` | Boolean search; `format=csv` for CSV. |
| `GET /api/fields` | Queryable fields, aliases, and allowed values. |
| `GET /api/calendar.ics?q=<query>` | Matching conferences as an iCalendar feed. |
| `GET /api/translate?q=<text>` | Natural language → boolean query via local Ollama (503 if none). |

`q` uses the web table's query language; an empty `q` matches everything.

### Deploy

Requires AWS credentials, the SAM CLI (`pip install aws-sam-cli`; no Docker
needed), and a VPC with two subnets in different AZs (the default VPC works).
The stack must be in **us-east-1** (its CloudFront WAF can only live there).

```bash
cd infra && sam build
sam deploy --stack-name conference-agent --region us-east-1 --resolve-s3 \
  --capabilities CAPABILITY_IAM --no-confirm-changeset \
  --parameter-overrides \
    VpcId=vpc-... SubnetIds=subnet-...,subnet-... \
    DBPassword=$(openssl rand -hex 16) \
    DbAdminCidr=$(curl -s ifconfig.me)/32
```

Optional parameters (see `infra/template.yaml` for all): `AlertEmail` /
`MonthlyBudgetUsd` (budget alert), `DomainName` + `AcmCertificateArn` (custom
domain; the certificate must be in us-east-1, then point DNS at the
`CloudFrontDomainName` output), `GaMeasurementId` (Google Analytics), and
`EnableScheduledRefresh=true` + `AnthropicApiKey` (scheduled discovery via
EventBridge).

The first deploy takes about 10 minutes and prints `WebsiteUrl`. The schema is
created on first request, so the site is empty until data is loaded.

### Load data

Push the local database into RDS (reads the connection string from the Lambda's
environment; needs `DbAdminCidr` to include your IP):

```bash
scripts/deploy.sh                # push local DB -> RDS
DRY_RUN=1 scripts/deploy.sh      # report the row count only
DEPLOY_CODE=1 scripts/deploy.sh  # also rebuild and update the Lambda code
```

Or run discovery against RDS directly:

```bash
pip install -e ".[discover,web]"   # web provides the pg8000 driver
export CONFERENCE_DATABASE_URL="postgresql+pg8000://conf_admin:<password>@<DatabaseEndpoint>:5432/conferences"
conference-agent discover --subcategory radiology
```

Both are idempotent: rows are matched by conference name.

### Tear down

```bash
aws cloudformation delete-stack --stack-name conference-agent --region us-east-1
```

RDS takes a final snapshot before deletion.
