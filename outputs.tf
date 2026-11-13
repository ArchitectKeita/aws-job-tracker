output "app_url" {
  description = "Open this in a browser"
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "access_key" {
  description = "Paste into the web app once. Read with: terraform output -raw access_key"
  value       = random_password.api_key.result
  sensitive   = true
}

output "next_step" {
  value = "Check ${var.notify_email} for an AWS Notification email and click Confirm subscription, or no reminders will arrive."
}

output "gmail_api_url" {
  description = "Paste into the Gmail script's API_URL property"
  value       = "${aws_apigatewayv2_stage.default.invoke_url}ingest"
}
