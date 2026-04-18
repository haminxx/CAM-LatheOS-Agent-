#!/usr/bin/env bash
# One-shot AWS bootstrap for CAM Cloud Proxy.
#
# What it does, in order, idempotently:
#   1. Sanity-check AWS credentials & region.
#   2. Create the Terraform state bucket + DynamoDB lock table (if missing).
#   3. Build and push the cam-proxy Docker image to ECR (skipped if
#      --skip-image is passed, e.g. before the ECR repo has been applied).
#   4. Print the exact terraform.tfvars snippet you need to fill in.
#
# It does NOT create a VPC, a Route53 zone, or an ACM cert — those are
# one-click operations in the console and encoding them here would double
# the script's surface area for little gain. See SETUP.md §1 and §3.
#
# Usage:
#   scripts/aws-bootstrap.sh                   # full run
#   scripts/aws-bootstrap.sh --skip-image      # infra-only (first time)
#   scripts/aws-bootstrap.sh --region us-west-2

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
SKIP_IMAGE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --region) REGION="$2"; shift 2 ;;
        --skip-image) SKIP_IMAGE=1; shift ;;
        -h|--help)
            sed -n '2,20p' "$0"
            exit 0
            ;;
        *) echo "unknown flag: $1" >&2; exit 1 ;;
    esac
done

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

command -v aws       >/dev/null || die "aws CLI not installed"
command -v terraform >/dev/null || die "terraform not installed"
command -v docker    >/dev/null || die "docker not installed"

# --- 1. credentials -----------------------------------------------------------
say "Verifying AWS credentials (region=${REGION})..."
CALLER=$(aws sts get-caller-identity --region "${REGION}")
ACCOUNT=$(echo "${CALLER}" | grep -oE '"Account": "[0-9]+"' | cut -d'"' -f4)
[[ -n "${ACCOUNT}" ]] || die "could not resolve AWS account id"
echo "    account: ${ACCOUNT}"
echo "    arn:     $(echo "${CALLER}" | grep -oE '"Arn": "[^"]+"' | cut -d'"' -f4)"

# --- 2. terraform backend -----------------------------------------------------
BUCKET="cam-tfstate-${ACCOUNT}"
LOCK_TABLE="cam-tfstate-locks"

say "Ensuring Terraform state backend (bucket=${BUCKET})..."
pushd "$(dirname "$0")/../infra/terraform/bootstrap" >/dev/null
terraform init -input=false
terraform apply -auto-approve \
    -var "state_bucket_name=${BUCKET}" \
    -var "lock_table_name=${LOCK_TABLE}" \
    -var "aws_region=${REGION}"
popd >/dev/null

# --- 3. image -----------------------------------------------------------------
if [[ "${SKIP_IMAGE}" -eq 1 ]]; then
    say "Skipping image build/push (per --skip-image)."
else
    REPO_URI="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/cam-proxy"
    say "Logging Docker into ECR..."
    aws ecr get-login-password --region "${REGION}" \
        | docker login --username AWS --password-stdin "${REPO_URI%/*}"

    if ! aws ecr describe-repositories --repository-names cam-proxy --region "${REGION}" >/dev/null 2>&1; then
        echo "    ECR repo 'cam-proxy' does not exist yet — run terraform apply"
        echo "    once (with --skip-image first, if this is the very first run)"
        echo "    then re-run this script without --skip-image."
        exit 0
    fi

    say "Building cam-proxy image..."
    pushd "$(dirname "$0")/.." >/dev/null
    docker build -t cam-proxy:latest .
    docker tag   cam-proxy:latest "${REPO_URI}:latest"
    SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "dev")
    docker tag   cam-proxy:latest "${REPO_URI}:${SHA}"
    docker push  "${REPO_URI}:latest"
    docker push  "${REPO_URI}:${SHA}"
    popd >/dev/null
    echo "    pushed: ${REPO_URI}:latest"
    echo "    pushed: ${REPO_URI}:${SHA}"
fi

# --- 4. next-steps printout ---------------------------------------------------
cat <<EOF

$(printf '\033[1;32m')✓ Bootstrap complete.$(printf '\033[0m')

Next:

  1. If you haven't already, create a VPC (SETUP.md §3) and request an ACM
     certificate for your CAM hostname (SETUP.md §1).

  2. Populate $(printf '\033[1minfra/terraform/terraform.tfvars\033[0m') using
     terraform.tfvars.example as a template. Minimum fields:
         vpc_id, public_subnet_ids, private_subnet_ids,
         acm_certificate_arn, dns_zone_id, dns_name, image_uri.

  3. terraform -chdir=infra/terraform init \\
         -backend-config="bucket=${BUCKET}" \\
         -backend-config="region=${REGION}" \\
         -backend-config="dynamodb_table=${LOCK_TABLE}"
     terraform -chdir=infra/terraform apply

  4. Populate SSM vendor secrets (SETUP.md §5).

  5. Provision your first hardware token:
         make tokens-init
         make tokens-provision USER=<you> TIER=standard QUOTA=600

EOF
