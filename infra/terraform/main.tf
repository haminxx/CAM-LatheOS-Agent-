################################################################################
# CAM Cloud Proxy — AWS infrastructure.
#
# Topology:
#
#   Internet ─▶ ALB (HTTPS 443, WebSocket upgrade) ─▶ EC2 ASG (private subnets)
#                                                          │
#                                                          ▼
#                                                  DynamoDB (HardwareTokens)
#                                                          │
#                                                          ▼
#                                                  SSM Parameter Store
#                                                  (vendor secrets, /cam/<env>/*)
#
# Single AZ outage tolerance, horizontal scaling on WebSocket count.
# Nothing here stores PII; audio is in-flight only.
################################################################################

locals {
  name = "cam-proxy-${var.environment}"
}

data "aws_ami" "al2023_arm" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-arm64"]
  }
  filter {
    name   = "state"
    values = ["available"]
  }
}

# ---- DynamoDB ---------------------------------------------------------------

resource "aws_dynamodb_table" "hardware_tokens" {
  name         = "CAM_HardwareTokens_${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "token"

  attribute {
    name = "token"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}

# ---- SSM parameter namespace (vendor keys) ----------------------------------
# Values are *not* declared here — they are written once via the console or
# the `aws ssm put-parameter --type SecureString` CLI. Terraform just owns the
# read path (IAM grant) and the namespace.

resource "aws_ssm_parameter" "placeholder" {
  # A no-op parameter whose only purpose is to stabilise the path prefix,
  # so IAM policies can reference /cam/<env>/* even before secrets are
  # populated.
  name  = "/cam/${var.environment}/_bootstrap"
  type  = "String"
  value = "ok"
  lifecycle {
    ignore_changes = [value]
  }
}

# ---- Security groups --------------------------------------------------------

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public ALB for CAM Cloud Proxy"
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTPS from the internet"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "ec2" {
  name        = "${local.name}-ec2"
  description = "EC2 nodes running the cam-proxy container"
  vpc_id      = var.vpc_id

  ingress {
    description     = "ALB -> uvicorn"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---- IAM --------------------------------------------------------------------

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ec2" {
  name               = "${local.name}-ec2"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

data "aws_iam_policy_document" "runtime" {
  statement {
    sid     = "ReadVendorSecrets"
    actions = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = [
      "arn:aws:ssm:${var.aws_region}:*:parameter/cam/${var.environment}/*",
    ]
  }
  statement {
    sid     = "HardwareTokenLookup"
    actions = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.hardware_tokens.arn]
  }
  statement {
    sid       = "PullImage"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
    ]
    resources = ["*"]
  }
  statement {
    sid     = "CloudWatchLogs"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "runtime" {
  role   = aws_iam_role.ec2.id
  policy = data.aws_iam_policy_document.runtime.json
}

resource "aws_iam_role_policy_attachment" "ssm_managed" {
  role       = aws_iam_role.ec2.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ec2" {
  name = "${local.name}-ec2"
  role = aws_iam_role.ec2.name
}

# ---- Launch template + ASG --------------------------------------------------

resource "aws_launch_template" "proxy" {
  name_prefix   = "${local.name}-"
  image_id      = data.aws_ami.al2023_arm.id
  instance_type = var.instance_type

  iam_instance_profile {
    arn = aws_iam_instance_profile.ec2.arn
  }

  vpc_security_group_ids = [aws_security_group.ec2.id]

  user_data = base64encode(templatefile("${path.module}/../ec2-userdata.sh", {
    IMAGE_URI   = var.image_uri
    ENVIRONMENT = var.environment
  }))

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  tag_specifications {
    resource_type = "instance"
    tags = { Name = local.name }
  }
}

resource "aws_autoscaling_group" "proxy" {
  name                      = local.name
  min_size                  = var.asg_min_size
  max_size                  = var.asg_max_size
  desired_capacity          = var.asg_desired_capacity
  vpc_zone_identifier       = var.private_subnet_ids
  target_group_arns         = [aws_lb_target_group.proxy.arn]
  health_check_type         = "ELB"
  health_check_grace_period = 90

  launch_template {
    id      = aws_launch_template.proxy.id
    version = "$Latest"
  }

  instance_refresh {
    strategy = "Rolling"
    preferences {
      min_healthy_percentage = 66
    }
  }

  tag {
    key                 = "Name"
    value               = local.name
    propagate_at_launch = true
  }
}

# ---- ALB --------------------------------------------------------------------

resource "aws_lb" "proxy" {
  name               = local.name
  internal           = false
  load_balancer_type = "application"
  subnets            = var.public_subnet_ids
  security_groups    = [aws_security_group.alb.id]

  idle_timeout = 3600  # long-lived WebSockets
}

resource "aws_lb_target_group" "proxy" {
  name        = local.name
  port        = 8080
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "instance"

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  stickiness {
    type            = "lb_cookie"
    enabled         = true
    cookie_duration = 3600
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.proxy.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.acm_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.proxy.arn
  }
}
