# AWS infrastructure (Terraform)

Terraform for Perf&Scale-owned AWS resources. Currently: the S3 bucket used by Kanary probe runs (push) and Jenkins (pull) for [KONFLUX-15649](https://redhat.atlassian.net/browse/KONFLUX-15649).

## AWS account

| Setting | Value |
|---|---|
| Account | `pco-aws-konflux-test-perfscale` |
| Account ID | `992382442726` |
| Region | `eu-west-1` |

IAM users for S3 access (`s3-staging-user`, `s3-prod-user`) are managed outside this module. Credentials live in Stone Soup Vault at `stonesoup/staging/perfscale/shared`.

## Prerequisites

- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.5
- AWS credentials with permission to create S3 buckets in the account above (ping your manager if you need the account secret)

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=eu-west-1
```

## Apply

```bash
cd infra

# Optional: copy and edit variable overrides
cp terraform.tfvars.example terraform.tfvars

terraform init
terraform plan
terraform apply
```

After apply, note the outputs (bucket name, region). Share them with Faisal for Kanary cronjob `--param s3-bucket=...` and with Jan for the Jenkins pull job.

## DPP pruner — read before apply

A manually created bucket named `konflux-perfscale-artifacts` was deleted on 2026-09-11 by automated `dpp-pruner` (CloudTrail `DeleteBucket`). **Confirm required exemption tags with Infra before running `terraform apply`**, then add them to `tags` in `terraform.tfvars`.

Default tags in `variables.tf` are a starting point only (`cost-center=670` was not sufficient on its own).

## What this creates

- S3 bucket (name from `bucket_name`, default `konflux-perfscale-artifacts`)
- Block all public access
- Default encryption (SSE-S3 AES256)
- Lifecycle rule: expire objects after 30 days; abort incomplete multipart uploads after 7 days

## Artifact layout (for Jenkins / consumers)

Kanary uploads a tarball per run (see [infra-deployments PR #13787](https://github.com/redhat-appstudio/infra-deployments/pull/13787)):

```
s3://<bucket>/<tested-cluster>/<test-type>/run-<tested-cluster>-<timestamp>.tar.gz
```

Example:

```
s3://konflux-perfscale-artifacts/konflux-perfscale-4-tenant/loadtest/run-konflux-perfscale-4-tenant-20260910T143000Z.tar.gz
```

The tarball contains the contents of `/tmp/artifacts/results/` from the probe run (including `load-test.json`).

## Smoke test after apply

```bash
cd ../tools/s3-artifact-collector
pip install -r requirements.txt

python s3_tools.py push \
  --bucket "$(terraform -chdir=../infra output -raw bucket_name)" \
  --local /tmp/test.json \
  --remote konflux-perfscale-4-tenant/loadtest/run-test.json

python s3_tools.py list \
  --bucket "$(terraform -chdir=../infra output -raw bucket_name)" \
  --prefix konflux-perfscale-4-tenant/
```

## State

Terraform state is stored locally in `terraform.tfstate` (gitignored). For team use, consider a remote backend (S3 + DynamoDB) later.
