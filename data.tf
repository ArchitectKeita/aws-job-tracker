resource "aws_dynamodb_table" "apps" {
  name         = "${var.project}-apps"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "app_id"

  attribute {
    name = "app_id"
    type = "S"
  }

  # Job search history is worth keeping: restore to any second in the last 35 days.
  point_in_time_recovery {
    enabled = true
  }
}

resource "aws_sns_topic" "digest" {
  name = "${var.project}-digest"
}

# Terraform creates the subscription, but AWS still emails a confirmation
# link -- nothing is delivered until it's clicked.
resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.digest.arn
  protocol  = "email"
  endpoint  = var.notify_email
}

# One shared key for the web app, generated here, never typed into code.
resource "random_password" "api_key" {
  length  = 32
  special = false
}
