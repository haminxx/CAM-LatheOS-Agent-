output "alb_dns_name" {
  description = "Public DNS of the ALB. CNAME your Route53 record here."
  value       = aws_lb.proxy.dns_name
}

output "alb_zone_id" {
  value = aws_lb.proxy.zone_id
}

output "ws_endpoint" {
  description = "WebSocket endpoint for LatheOS clients."
  value       = "wss://${aws_lb.proxy.dns_name}/ws/cam"
}

output "dynamodb_table" {
  value = aws_dynamodb_table.hardware_tokens.name
}

output "ssm_namespace" {
  description = "Populate SSM SecureString parameters under this prefix."
  value       = "/cam/${var.environment}/"
}
