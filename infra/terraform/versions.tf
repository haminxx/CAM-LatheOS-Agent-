terraform {
  required_version = ">= 1.7.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }

  # Remote state. Bucket + lock table are provisioned by `infra/terraform/bootstrap`
  # (run that first — it is self-contained and uses local state). Override any of
  # the values below with `-backend-config=...` flags if you deploy per-environment.
  backend "s3" {
    bucket         = "haminxx-cam-tfstate"
    key            = "cam-cloud-proxy/prod.tfstate"
    region         = "us-east-1"
    dynamodb_table = "haminxx-cam-tflock"
    encrypt        = true
  }
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
