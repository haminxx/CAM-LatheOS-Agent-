################################################################################
# ECR repository for the cam-proxy image + GitHub Actions OIDC federation so
# CI pushes images without long-lived AWS access keys.
################################################################################

variable "github_owner" {
  type        = string
  description = "GitHub org/user that hosts the cam-proxy repository."
  default     = "haminxx"
}

variable "github_repo" {
  type        = string
  description = "Repository name on GitHub."
  default     = "CAM-LatheOS-Agent-"
}

variable "github_branch_ref" {
  type        = string
  description = "Which ref is allowed to push images. `main` is the default gate."
  default     = "refs/heads/main"
}

# ---- ECR --------------------------------------------------------------------

resource "aws_ecr_repository" "proxy" {
  name                 = "cam-proxy"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "proxy" {
  repository = aws_ecr_repository.proxy.name
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Retain only the last 30 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 30
        }
        action = { type = "expire" }
      }
    ]
  })
}

# ---- GitHub OIDC ------------------------------------------------------------

data "aws_iam_openid_connect_provider" "github" {
  # Exists if someone already added GitHub OIDC at account scope. If not, flip
  # the commented resource block below on first apply and delete this data
  # source afterwards (OIDC providers are account-global singletons).
  url = "https://token.actions.githubusercontent.com"
}

# resource "aws_iam_openid_connect_provider" "github" {
#   url             = "https://token.actions.githubusercontent.com"
#   client_id_list  = ["sts.amazonaws.com"]
#   thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
# }

data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "gha_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_owner}/${var.github_repo}:ref:${var.github_branch_ref}"]
    }
  }
}

resource "aws_iam_role" "gha_ecr_push" {
  name               = "${local.name}-gha-ecr-push"
  assume_role_policy = data.aws_iam_policy_document.gha_assume.json
  description        = "Assumed by GitHub Actions to push cam-proxy images to ECR."
}

data "aws_iam_policy_document" "gha_ecr_push" {
  statement {
    sid       = "GetAuthToken"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid = "PushPull"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [aws_ecr_repository.proxy.arn]
  }
}

resource "aws_iam_role_policy" "gha_ecr_push" {
  role   = aws_iam_role.gha_ecr_push.id
  policy = data.aws_iam_policy_document.gha_ecr_push.json
}

output "ecr_repository_url" {
  value = aws_ecr_repository.proxy.repository_url
}

output "gha_ecr_role_arn" {
  description = "Set this as AWS_ROLE_TO_ASSUME secret in GitHub Actions."
  value       = aws_iam_role.gha_ecr_push.arn
}
