variable "aws_region" {
  description = "AWS region for the probe-run artifacts bucket."
  type        = string
  default     = "eu-west-1"
}

variable "bucket_name" {
  description = "S3 bucket name for Kanary probe-run artifacts."
  type        = string
  default     = "konflux-perfscale-artifacts"
}

variable "artifact_retention_days" {
  description = "Number of days before S3 objects are expired by the lifecycle rule."
  type        = number
  default     = 30
}

variable "tags" {
  description = <<-EOT
    Tags applied to the bucket. Required tags must include whatever DPP pruner
    expects in pco-aws-konflux-test-perfscale — confirm with Infra before apply.
    See KONFLUX-15649 and infra/README.md.
  EOT
  type = map(string)
  default = {
    cost-center = "670"
    owner       = "perf-scale"
    purpose     = "probe-run-artifacts"
    jira        = "KONFLUX-15649"
  }
}
