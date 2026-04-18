#!/usr/bin/env bash
# EC2 user-data bootstrap for a CAM Cloud Proxy node.
#
# Rendered by Terraform via `templatefile()` — $${...} escapes shell vars,
# plain $${VAR} is interpolated by Terraform.  Variables injected:
#   IMAGE_URI   — ECR URI of the cam-proxy image (including tag)
#   ENVIRONMENT — dev | staging | prod
#
# Amazon Linux 2023, arm64 by default. Installs Docker, pulls SSM secrets,
# runs the container with health-check wiring to /healthz on :8080.
set -euo pipefail

IMAGE_URI="${IMAGE_URI}"
ENVIRONMENT="${ENVIRONMENT}"

dnf update -y
dnf install -y docker awscli jq
systemctl enable --now docker

# ECR auth (instance profile must have ecr:GetAuthorizationToken).
REGION="$${AWS_DEFAULT_REGION:-us-east-1}"
aws ecr get-login-password --region "$${REGION}" \
  | docker login --username AWS --password-stdin "$${IMAGE_URI%%/*}"

docker pull "$${IMAGE_URI}"

# Hydrate env file from SSM Parameter Store. Each param under
# /cam/<env>/* becomes an uppercased env key in /etc/cam.env.
: > /etc/cam.env
chmod 600 /etc/cam.env

aws ssm get-parameters-by-path \
    --path "/cam/$${ENVIRONMENT}/" \
    --with-decryption \
    --region "$${REGION}" \
    --query "Parameters[*].[Name,Value]" \
    --output json \
  | jq -r '.[] | "\(.[0])=\(.[1])"' \
  | while IFS='=' read -r name value; do
      key="$${name##*/}"
      # Skip bootstrap sentinel.
      [[ "$${key}" == "_bootstrap" ]] && continue
      echo "$${key^^}=$${value}" >> /etc/cam.env
    done

echo "ENV=$${ENVIRONMENT}" >> /etc/cam.env

docker run -d --restart=always \
  --name cam-proxy \
  --log-driver=awslogs \
  --log-opt awslogs-group="/cam/$${ENVIRONMENT}" \
  --log-opt awslogs-create-group=true \
  -p 8080:8080 \
  --env-file /etc/cam.env \
  "$${IMAGE_URI}"
