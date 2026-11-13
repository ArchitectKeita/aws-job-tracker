data "aws_iam_policy_document" "scheduler_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.project}-scheduler-role"
  assume_role_policy = data.aws_iam_policy_document.scheduler_trust.json
}

resource "aws_iam_role_policy" "scheduler" {
  name = "invoke-digest"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.digest.arn
    }]
  })
}

# Every morning: nudge about applications that have gone quiet.
resource "aws_scheduler_schedule" "daily" {
  name                         = "${var.project}-daily-followups"
  schedule_expression          = "cron(0 9 * * ? *)"
  schedule_expression_timezone = var.timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.digest.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ mode = "daily" })
  }
}

# Sunday evening: the week in numbers.
resource "aws_scheduler_schedule" "weekly" {
  name                         = "${var.project}-weekly-summary"
  schedule_expression          = "cron(0 18 ? * SUN *)"
  schedule_expression_timezone = var.timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.digest.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ mode = "weekly" })
  }
}
