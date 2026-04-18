terraform {
  required_version = ">= 1.7.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }

  # Swap to your own S3 + DynamoDB lock table before `terraform init`.
  # backend "s3" {
  #   bucket         = "haminxx-terraform-state"
  #   key            = "cam-cloud-proxy/prod.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "terraform-locks"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project     = "CAM"
      Component   = "cloud-proxy"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
