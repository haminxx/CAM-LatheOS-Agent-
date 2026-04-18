variable "aws_region" {
  description = "AWS region to deploy CAM Cloud Proxy into."
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment tier — drives tagging and SSM path prefix."
  type        = string
  default     = "prod"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "vpc_id" {
  description = "Existing VPC id. Create one with `aws-vpc` module before applying."
  type        = string
}

variable "public_subnet_ids" {
  description = "Public subnets for the ALB (min 2, multi-AZ)."
  type        = list(string)
  validation {
    condition     = length(var.public_subnet_ids) >= 2
    error_message = "ALB requires subnets in at least 2 AZs."
  }
}

variable "private_subnet_ids" {
  description = "Private subnets for the EC2 ASG (min 2, multi-AZ)."
  type        = list(string)
}

variable "instance_type" {
  description = "EC2 instance type. c7g.large gives best $/WS on Graviton."
  type        = string
  default     = "c7g.large"
}

variable "image_uri" {
  description = "ECR URI of the cam-proxy container image (includes tag)."
  type        = string
}

variable "acm_certificate_arn" {
  description = "ACM TLS cert for the ALB HTTPS listener."
  type        = string
}

variable "asg_min_size" {
  type    = number
  default = 2
}

variable "asg_max_size" {
  type    = number
  default = 10
}

variable "asg_desired_capacity" {
  type    = number
  default = 2
}
