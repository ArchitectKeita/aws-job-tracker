variable "region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  description = "Name prefix for every resource"
  type        = string
  default     = "job-tracker"
}

variable "notify_email" {
  description = "Where follow-up reminders and weekly summaries go. Set it in terraform.tfvars, which git ignores."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.notify_email))
    error_message = "notify_email must be an email address."
  }
}

variable "stale_days" {
  description = "Days at 'applied' with no movement before a follow-up reminder"
  type        = number
  default     = 7
}

variable "timezone" {
  description = "Time zone the schedules run in"
  type        = string
  default     = "America/New_York"
}

variable "model_id" {
  description = "Bedrock model that reads application emails. If it's unavailable, pattern-matching takes over."
  type        = string
  # Cross-region inference profile: its own token allowance, separate from the
  # single-region one that new accounts get almost none of.
  default = "us.amazon.nova-micro-v1:0"
}
