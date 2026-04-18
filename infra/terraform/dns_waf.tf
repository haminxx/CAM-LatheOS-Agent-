################################################################################
# Public DNS + WAF for the CAM Cloud Proxy ALB.
#
# The WAF ACL rate-limits per-IP on /ws/cam so a single LatheOS client (or a
# malicious actor) can't open unbounded parallel WebSockets and blow through
# our vendor quotas.
################################################################################

variable "dns_zone_id" {
  type        = string
  description = "Route53 hosted-zone id that owns `dns_name`. Empty string to skip DNS."
  default     = ""
}

variable "dns_name" {
  type        = string
  description = "Public hostname, e.g. cam.latheos.example.com. Empty string to skip DNS."
  default     = ""
}

variable "ws_rate_limit_per_5min" {
  type        = number
  description = "Max requests per 5-min window per source IP on /ws/cam."
  default     = 300
}

# ---- Route53 ----------------------------------------------------------------

resource "aws_route53_record" "alb" {
  count   = var.dns_zone_id != "" && var.dns_name != "" ? 1 : 0
  zone_id = var.dns_zone_id
  name    = var.dns_name
  type    = "A"

  alias {
    name                   = aws_lb.proxy.dns_name
    zone_id                = aws_lb.proxy.zone_id
    evaluate_target_health = true
  }
}

# ---- WAF v2 (regional, attached to ALB) ------------------------------------

resource "aws_wafv2_web_acl" "proxy" {
  name        = "${local.name}-acl"
  scope       = "REGIONAL"
  description = "Rate-limit /ws/cam and block AWS-managed bad actors."

  default_action {
    allow {}
  }

  # 1. Rate-limit any IP hitting /ws/cam.
  rule {
    name     = "rate-limit-ws-cam"
    priority = 1

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = var.ws_rate_limit_per_5min
        aggregate_key_type = "IP"

        scope_down_statement {
          byte_match_statement {
            search_string         = "/ws/cam"
            positional_constraint = "STARTS_WITH"
            field_to_match {
              uri_path {}
            }
            text_transformation {
              priority = 0
              type     = "LOWERCASE"
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-rate-ws-cam"
      sampled_requests_enabled   = true
    }
  }

  # 2. Common managed rule set (SQLi, XSS, etc). Cheap and universal.
  rule {
    name     = "aws-common"
    priority = 2

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-managed-common"
      sampled_requests_enabled   = true
    }
  }

  # 3. IP reputation — known malicious scanners.
  rule {
    name     = "aws-ip-reputation"
    priority = 3

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesAmazonIpReputationList"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-ip-reputation"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.name}-acl"
    sampled_requests_enabled   = true
  }
}

resource "aws_wafv2_web_acl_association" "proxy" {
  resource_arn = aws_lb.proxy.arn
  web_acl_arn  = aws_wafv2_web_acl.proxy.arn
}

output "public_hostname" {
  description = "Set this as WS endpoint in LatheOS config once DNS propagates."
  value       = var.dns_name != "" ? var.dns_name : aws_lb.proxy.dns_name
}
