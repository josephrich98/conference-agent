# Deploying conference-agent (FastAPI + Lambda + PostgreSQL)

The web table runs as a FastAPI app on AWS Lambda, fronted by a CloudFront
distribution and backed by an RDS PostgreSQL database. The Lambda and the
database share a VPC. Because the rest of the code is plain SQLAlchemy, the only
thing that changes between local SQLite and production Postgres is the connection
string.

```
Browser ──HTTPS──> CloudFront ──(OAC, SigV4)──> Lambda Function URL ──> FastAPI (Mangum) ──VPC──> RDS PostgreSQL
```

- **CloudFront + Origin Access Control.** The Function URL is set to
  `AuthType: AWS_IAM`, so it is *not* publicly reachable on its `*.lambda-url`
  host. CloudFront signs each origin request (SigV4) via an Origin Access
  Control, and a scoped `lambda:InvokeFunctionUrl` permission lets that one
  distribution through — so all public traffic enters through CloudFront. This
  also gives you HTTPS, an IPv6 / HTTP3 edge, and a place to attach a custom
  domain (see [Custom domain](#custom-domain-optional)).

## Architecture notes

- **Driver:** the Lambda uses **pg8000** (pure Python), so `sam build` needs no
  Docker. Locally you can use `psycopg` instead (`pip install -e ".[postgres]"`).
- **Engine reuse:** `get_engine` caches the engine per URL with `pool_pre_ping`
  and a tiny pool, so warm Lambda containers reuse connections.
- **Calendar feed (`.ics`):** the "Subscribe (.ics)" button and the per-row
  `📅 cal` link hit `GET /api/calendar.ics`, which is pure Python — no third-party
  libraries and no credentials — so it works on the hosted Lambda. This is the
  only calendar path: end users subscribe to the feed URL (which mirrors the
  active search) from any calendar app (Google "Add by URL", Apple/Outlook "Add
  from URL"), or download a one-off `.ics`, with no sign-in. Discovery (which
  calls the Anthropic API) runs locally or in CI, not in the VPC Lambda.

## Prerequisites

- AWS credentials configured (`aws configure`) with permission to create the
  stack's resources (CloudFormation, Lambda, CloudFront, RDS, EC2 security
  groups, IAM, S3, CloudWatch Logs). For a personal account, the managed
  `AdministratorAccess`
  policy is the simplest; scope down for production.
- The [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
  (`pip install aws-sam-cli`). **Docker is not required** (pg8000 is pure Python).
- A VPC with at least two subnets in different AZs. Your **default VPC** works:

  ```bash
  aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
      --query 'Vpcs[0].VpcId' --output text
  aws ec2 describe-subnets --filters Name=vpc-id,Values=<VPC_ID> \
      --query 'Subnets[].SubnetId' --output text
  ```

## Build and deploy

```bash
cd infra
sam build
sam deploy --guided        # first time; prompts for parameters and saves them
```

Non-interactive, deploying with your IP allowed so you can load data afterward:

```bash
sam deploy --stack-name conference-agent --region us-west-2 --resolve-s3 \
  --capabilities CAPABILITY_IAM --no-confirm-changeset \
  --parameter-overrides \
    VpcId=vpc-0a1b2c3d SubnetIds=subnet-1111,subnet-2222 \
    DBPassword=$(openssl rand -hex 16) \
    DbAdminCidr=$(curl -s ifconfig.me)/32
```

| Parameter     | Example                         | Notes |
|---------------|---------------------------------|-------|
| `VpcId`       | `vpc-0a1b2c3d`                  | Default VPC is fine |
| `SubnetIds`   | `subnet-1111,subnet-2222`       | Two+ subnets, different AZs |
| `DBPassword`  | URL-safe, ≥ 8 chars             | Master password |
| `DbAdminCidr` | `203.0.113.4/32` (your IP)      | Optional; enables direct data loading. Omit to keep the DB private |

Provisioning RDS takes ~5–10 minutes on the first deploy (and CloudFront takes a
few more minutes to finish propagating to the edge). On success SAM prints:

```
WebsiteUrl          https://<distribution-id>.cloudfront.net/
CloudFrontDomainName <distribution-id>.cloudfront.net
DatabaseEndpoint    <id>.<region>.rds.amazonaws.com
```

`WebsiteUrl` is the CloudFront URL (the `*.lambda-url` host is now IAM-locked and
not browsable directly). The schema is created automatically on first request
(`Base.metadata.create_all`), so the site comes up empty until you load data.

## Custom domain (optional)

The stack ships on the default `*.cloudfront.net` URL and is pre-wired to take a
custom domain via two parameters — no template changes needed. When you have a
domain, go live in three steps:

1. **Request an ACM certificate for the name, in `us-east-1`.** CloudFront only
   reads certificates from `us-east-1`, regardless of where the rest of the stack
   lives. Validate it (DNS validation is easiest) and copy its ARN:

   ```bash
   aws acm request-certificate --region us-east-1 \
     --domain-name conferences.example.com \
     --validation-method DNS \
     --query CertificateArn --output text
   # add the CNAME it asks for, then wait for status ISSUED:
   aws acm describe-certificate --region us-east-1 \
     --certificate-arn <arn> --query 'Certificate.Status'
   ```

2. **Redeploy with the two parameters set.** This adds the domain as a CloudFront
   alias and swaps in your certificate:

   ```bash
   sam deploy --stack-name conference-agent --resolve-s3 \
     --capabilities CAPABILITY_IAM --no-confirm-changeset \
     --parameter-overrides \
       VpcId=... SubnetIds=... DBPassword=... \
       DomainName=conferences.example.com \
       AcmCertificateArn=arn:aws:acm:us-east-1:<acct>:certificate/<id>
   ```

3. **Point DNS at CloudFront.** Create a record for `DomainName` aimed at the
   stack's `CloudFrontDomainName` output — a **Route 53 alias** (A/AAAA) if the
   zone is in Route 53, or a plain **CNAME** at any other DNS host.

`WebsiteUrl` then reports your custom `https://` URL. Because the certificate ARN
is just a parameter, you can also register the domain in Route 53 and manage its
hosted zone entirely outside this stack — the app code and the rest of the
template are unaffected.

## Load data into RDS

Discovery calls the Anthropic API and writes to the database, so run it from your
laptop (or CI) with `CONFERENCE_DATABASE_URL` pointed at RDS. This requires the
DB to be reachable from your IP — deploy with `DbAdminCidr` set (above), or add a
temporary inbound rule to the DB security group.

```bash
pip install -e ".[discover,postgres]"
export ANTHROPIC_API_KEY=sk-ant-...
export CONFERENCE_DATABASE_URL="postgresql+psycopg://conf_admin:<password>@<DatabaseEndpoint>:5432/conferences"

conference-agent discover --category radiology   # populates RDS
```

Re-running is safe: ingestion is idempotent on the conference acronym. Refresh
the `WebsiteUrl` and the table appears.

### One-command reconcile + deploy

To push the **local** table into RDS without copying the password by hand, use
`scripts/deploy.sh`. It resolves the function name from the CloudFormation stack,
reads `CONFERENCE_DATABASE_URL` out of the Lambda's own environment, and runs the
idempotent `push_db.py` upsert. All inputs are environment variables (see the
header of the script for the full list and defaults):

```bash
scripts/deploy.sh                # push local DB -> RDS (data only)
DRY_RUN=1 scripts/deploy.sh      # report the row count, write nothing
DEPLOY_CODE=1 scripts/deploy.sh  # also `sam build` + update the Lambda code, then push
```

Defaults target the `conference-agent` stack in `us-east-1`; override with
`STACK_NAME` / `AWS_REGION`. The Vercel proxy is a static rewrite and is not
touched — redeploy it only if the CloudFront target changes.

> Note the driver in the URL: use `+psycopg` locally (from the `postgres` extra)
> or `+pg8000` (from the `deploy` extra). Either reaches the same database; the
> Lambda itself always uses `+pg8000`.

## Scheduled refresh against RDS

The daily refresh workflow (`.github/workflows/daily_update.yml`) can populate
RDS directly: set the `CONFERENCE_DATABASE_URL` secret to the RDS URL (with
`+pg8000` and a CI-allowed `DbAdminCidr`/security-group rule) and the run upserts
into Postgres instead of producing a local SQLite artifact.

## Tear down

```bash
aws cloudformation delete-stack --stack-name conference-agent
```

The database has `DeletionPolicy: Snapshot`, so a final snapshot is taken before
the instance is removed.
