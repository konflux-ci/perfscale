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

variable "pruner_preserve" {
  description = <<-EOT
    Required pruner-preserve tag value. Format: <kerberos>-<YYYY-MM-DD> or
    <kerberos>-never. The date is informational only. See infra/README.md.
  EOT
  type    = string
  default = "tdesu-never"
}

variable "tags" {
  description = "Extra tags applied to the bucket (pruner-preserve is set via pruner_preserve)."
  type        = map(string)
  default = {
    cost-center = "670"
    owner       = "perf-scale"
    purpose     = "probe-run-artifacts"
  }
}
