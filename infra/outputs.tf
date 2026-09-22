output "bucket_name" {
  description = "S3 bucket name — pass to Kanary cronjobs as --param s3-bucket=<name>."
  value       = aws_s3_bucket.probe_run_artifacts.id
}

output "bucket_arn" {
  description = "S3 bucket ARN."
  value       = aws_s3_bucket.probe_run_artifacts.arn
}

output "aws_region" {
  description = "AWS region for S3 API calls (AWS_REGION env var)."
  value       = var.aws_region
}

output "s3_endpoint" {
  description = "Regional S3 endpoint for documentation and clients."
  value       = "s3.${var.aws_region}.amazonaws.com"
}

output "artifact_key_prefix_example" {
  description = "Example object key from the Kanary upload task (infra-deployments PR #13787)."
  value       = "run-probe/<cluster>/<test-type>/run-probe-<cluster>-<test-type>-<YYYYMMDDTHHMMSSZ>.tar.gz"
}
