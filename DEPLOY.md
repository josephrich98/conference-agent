# Deploying conference-agent (FastAPI + Lambda + PostgreSQL)

The web table runs as a FastAPI app on AWS Lambda (behind a public Function URL),
backed by an RDS PostgreSQL database. The Lambda and the database share a VPC.
Because the rest of the code is plain SQLAlchemy, the only thing that changes
between local SQLite and production Postgres is the connection string.

```
Browser ──HTTPS──> Lambda Function URL ──> FastAPI (Mangum) ──VPC──> RDS PostgreSQL
```

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
  stack's resources (CloudFormation, Lambda, RDS, EC2 security groups, IAM, S3,
  CloudWatch Logs). For a personal account, the managed `AdministratorAccess`
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

Provisioning RDS takes ~5–10 minutes on the first deploy. On success SAM prints:

```
WebsiteUrl        https://<id>.lambda-url.<region>.on.aws/
DatabaseEndpoint  <id>.<region>.rds.amazonaws.com
```

The schema is created automatically on first request
(`Base.metadata.create_all`), so the site comes up empty until you load data.

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
