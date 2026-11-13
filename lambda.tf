data "archive_file" "api" {
  type        = "zip"
  source_dir  = "${path.module}/src/api"
  output_path = "${path.module}/build/api.zip"
}

data "archive_file" "digest" {
  type        = "zip"
  source_dir  = "${path.module}/src/digest"
  output_path = "${path.module}/build/digest.zip"
}

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# Log groups are created here, not by Lambda on first run, so they get a
# retention period -- and so `terraform destroy` removes them too.
resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${var.project}-api"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "digest" {
  name              = "/aws/lambda/${var.project}-digest"
  retention_in_days = 14
}

# --- API function --------------------------------------------------------------

resource "aws_iam_role" "api" {
  name               = "${var.project}-api-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "api" {
  statement {
    sid       = "ReadWriteApplications"
    actions   = ["dynamodb:Scan", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
    resources = [aws_dynamodb_table.apps.arn]
  }
  statement {
    sid       = "WriteOwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.api.arn}:*"]
  }
}

resource "aws_iam_role_policy" "api" {
  name   = "api"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

resource "aws_lambda_function" "api" {
  function_name    = "${var.project}-api"
  role             = aws_iam_role.api.arn
  runtime          = "python3.12"
  handler          = "app.lambda_handler"
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256
  timeout          = 10

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.apps.name
      API_KEY    = random_password.api_key.result
      STALE_DAYS = tostring(var.stale_days)
    }
  }

  depends_on = [aws_cloudwatch_log_group.api, aws_iam_role_policy.api]
}

# --- digest function -----------------------------------------------------------

resource "aws_iam_role" "digest" {
  name               = "${var.project}-digest-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "digest" {
  statement {
    sid       = "ReadApplicationsAndMarkReminded"
    actions   = ["dynamodb:Scan", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.apps.arn]
  }
  statement {
    sid       = "SendDigest"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.digest.arn]
  }
  statement {
    sid       = "WriteOwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.digest.arn}:*"]
  }
}

resource "aws_iam_role_policy" "digest" {
  name   = "digest"
  role   = aws_iam_role.digest.id
  policy = data.aws_iam_policy_document.digest.json
}

resource "aws_lambda_function" "digest" {
  function_name    = "${var.project}-digest"
  role             = aws_iam_role.digest.arn
  runtime          = "python3.12"
  handler          = "digest.lambda_handler"
  filename         = data.archive_file.digest.output_path
  source_code_hash = data.archive_file.digest.output_base64sha256
  timeout          = 30

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.apps.name
      TOPIC_ARN  = aws_sns_topic.digest.arn
      APP_URL    = aws_apigatewayv2_stage.default.invoke_url
      STALE_DAYS = tostring(var.stale_days)
    }
  }

  depends_on = [aws_cloudwatch_log_group.digest, aws_iam_role_policy.digest]
}

# --- ingest function (email -> tracker) -----------------------------------------
# Separate from the API function on purpose: it's the only one allowed to call
# Bedrock, so the web app can't run up model usage even if its key leaked.

data "archive_file" "ingest" {
  type        = "zip"
  source_dir  = "${path.module}/src/ingest"
  output_path = "${path.module}/build/ingest.zip"
}

resource "aws_cloudwatch_log_group" "ingest" {
  name              = "/aws/lambda/${var.project}-ingest"
  retention_in_days = 14
}

resource "aws_iam_role" "ingest" {
  name               = "${var.project}-ingest-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "ingest" {
  statement {
    sid       = "MatchAndRecordApplications"
    actions   = ["dynamodb:Scan", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.apps.arn]
  }
  statement {
    sid     = "ReadEmailsWithNova"
    actions = ["bedrock:InvokeModel"]
    resources = [
      "arn:aws:bedrock:*::foundation-model/amazon.nova-micro-v1:0",
      "arn:aws:bedrock:*:*:inference-profile/us.amazon.nova-micro-v1:0",
    ]
  }
  statement {
    sid       = "WriteOwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.ingest.arn}:*"]
  }
}

resource "aws_iam_role_policy" "ingest" {
  name   = "ingest"
  role   = aws_iam_role.ingest.id
  policy = data.aws_iam_policy_document.ingest.json
}

resource "aws_lambda_function" "ingest" {
  function_name    = "${var.project}-ingest"
  role             = aws_iam_role.ingest.arn
  runtime          = "python3.12"
  handler          = "ingest.lambda_handler"
  filename         = data.archive_file.ingest.output_path
  source_code_hash = data.archive_file.ingest.output_base64sha256
  timeout          = 20

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.apps.name
      API_KEY    = random_password.api_key.result
      MODEL_ID   = var.model_id
    }
  }

  depends_on = [aws_cloudwatch_log_group.ingest, aws_iam_role_policy.ingest]
}
