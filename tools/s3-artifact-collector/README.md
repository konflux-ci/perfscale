# S3 Artifact Collector

## Setup

### Requirements

```bash
pip install -r requirements.txt
```

### Environment Variables

The script reads AWS credentials from the environment. Set these before running:

| Variable | Description |
|---|---|
| `AWS_ACCESS_KEY_ID` | IAM access key ID |
| `AWS_SECRET_ACCESS_KEY` | IAM secret access key |
| `AWS_REGION` | AWS region (use `eu-west-1`) |

## Usage

```bash
# Push a file to S3
python s3_tools.py push --bucket BUCKET_NAME --local ./result.json --remote probe-runs/result.json

# List objects in the bucket
python s3_tools.py list --bucket BUCKET_NAME --prefix probe-runs/

# Download a file from S3
python s3_tools.py download --bucket BUCKET_NAME --remote probe-runs/result.json --local ./result.json

# Delete a file from S3
python s3_tools.py delete --bucket BUCKET_NAME --remote probe-runs/result.json
```

Add `--verbose` to any command for debug logging.

## S3 Lifecycle Policy

To prevent the bucket from growing indefinitely, configure an S3 lifecycle rule to auto-expire objects after a retention period. This can be set in the AWS console (S3 > Bucket > Management > Lifecycle rules) or via the CLI:

```json
{
    "Rules": [
        {
            "ID": "expire-old-artifacts",
            "Status": "Enabled",
            "Filter": {
                "Prefix": ""
            },
            "Expiration": {
                "Days": 30
            }
        }
    ]
}
```

Apply with a JSON file:

```bash
aws s3api put-bucket-lifecycle-configuration \
    --bucket YOUR_BUCKET_NAME \
    --lifecycle-configuration file://lifecycle.json
```

Or inline:

```bash
aws s3api put-bucket-lifecycle-configuration \
    --bucket konflux-perfscale-artifacts-poc \
    --lifecycle-configuration '{"Rules":[{"ID":"expire-old-artifacts","Status":"Enabled","Filter":{"Prefix":""},"Expiration":{"Days":30}}]}'
```

This deletes all objects older than 30 days. Adjust the `Days` value based on how long artifacts need to be retained for processing.

## AWS Account Info

- Account ID: `992382442726`
- Region: `eu-west-1` (Ireland)
- CI service account: `prow-service-account` (credentials in OpenShift CI Vault)
