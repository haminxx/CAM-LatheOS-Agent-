# LatheOS + CAM — end-to-end setup guide

This is the *zero-to-working* runbook for the full stack. If you follow every
step top-to-bottom you will end with:

- a CAM Cloud Proxy serving WebSockets on your own domain,
- a DynamoDB row for your first hardware token,
- a LatheOS installer ISO flashed to a USB,
- a machine that says "Hey CAM → docker compose up -d" and actually runs it.

The stack is intentionally split across two repositories:

| Repository | Purpose | Runs on |
|---|---|---|
| [`haminxx/CAM-LatheOS-Agent-`](https://github.com/haminxx/CAM-LatheOS-Agent-) | Cloud proxy, auth, vendor routing, infra-as-code | AWS (EC2 + ALB) |
| [`haminxx/LatheOS`](https://github.com/haminxx/LatheOS) | Declarative OS, Sway UI, local daemon, installer | Your NVMe |

They are wired together by **one** thing: a 32-char hardware token in
`/persist/secrets/cam.env` on the target drive. Everything else flows from
that.

---

## 0. What you need before you touch any code

Accounts you will open (all have free tiers big enough to test):

| Service | Why | How to sign up |
|---|---|---|
| **AWS** | Hosts the cloud proxy, DynamoDB, ACM, Route53 | <https://aws.amazon.com/> (new accounts get $100 credit) |
| **Deepgram** | Streaming speech-to-text | <https://console.deepgram.com/> (200 hours free) |
| **Groq** *or* **xAI** | LLM (pick one; Groq is faster, xAI reasons deeper) | <https://console.groq.com/> / <https://console.x.ai/> |
| **Cartesia** | Streaming text-to-speech (CAM's voice) | <https://play.cartesia.ai/> |
| **Picovoice** | Wake word "Hey CAM" on-device | <https://console.picovoice.ai/> (free for personal) |
| **A domain name** | Needed for a TLS cert | Namecheap / Cloudflare / Route53 registrar |
| **GitHub** | Holds the repos, runs CI, publishes ISO | already done |

Tools on your laptop (install once):

```
aws CLI v2      # https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html
terraform >= 1.6
docker
python 3.12+
git
```

On Windows, the PowerShell versions of `aws` and `terraform` work fine; you
do **not** need WSL for any of these. WSL is only needed if you want to run
`nix build` locally instead of relying on CI artifacts.

---

## 1. Point a domain at AWS (one-time, ~10 min)

You need a public hostname — e.g. `cam.latheos.dev` — so the LatheOS daemon
can open `wss://cam.latheos.dev/ws/cam` with a valid certificate.

```bash
aws route53 create-hosted-zone \
    --name "latheos.dev" \
    --caller-reference "$(date +%s)"
```

Copy the four nameservers the command prints and set them at your domain
registrar. DNS propagation takes 1–30 min.

Then request an ACM certificate **in the same region** you'll deploy the
proxy into (start with `us-east-1` unless you have a reason otherwise):

```bash
aws acm request-certificate \
    --domain-name "cam.latheos.dev" \
    --validation-method DNS \
    --region us-east-1
```

The console (or `aws acm describe-certificate`) will give you a CNAME to add
to Route53 to prove you own the domain. The cert issues automatically in
~2 min once that record exists. **Save the certificate ARN** — you'll pass it
into Terraform.

---

## 2. Create the state bucket (Terraform's brain) — 2 min

Terraform needs a place to store the `tfstate` file so it remembers what
it deployed. We do this with a tiny, self-contained bootstrap.

```bash
git clone https://github.com/haminxx/CAM-LatheOS-Agent-.git cam
cd cam
cd infra/terraform/bootstrap
terraform init
terraform apply \
    -var "bucket=cam-tfstate-$(aws sts get-caller-identity --query Account --output text)"
```

That creates an S3 bucket + DynamoDB lock table. Write down the bucket name;
you will *never* hand-edit anything inside it.

---

## 3. Deploy the cloud proxy infrastructure — 10 min

You need a VPC with at least two public and two private subnets across
different AZs. If you already have one, skip to the terraform step. If you
don't, the fastest path is the AWS VPC wizard:

```
AWS Console → VPC → Create VPC → "VPC and more"
  Name: cam
  IPv4 CIDR: 10.20.0.0/16
  AZs: 2
  Public subnets: 2
  Private subnets: 2
  NAT gateways: 1 per AZ   ← pick this
  VPC endpoints: none      ← fine for now
```

Click Create; it finishes in ~3 min. Grab the VPC id and the two public /
two private subnet ids from the resource map.

Now apply the proxy stack:

```bash
cd ../            # back to infra/terraform
cp terraform.tfvars.example terraform.tfvars   # (or create by hand — see below)
terraform init -backend-config="bucket=cam-tfstate-<your-account-id>"
terraform plan
terraform apply
```

`terraform.tfvars` contents — fill these with values from steps 1 and the VPC
wizard:

```hcl
aws_region         = "us-east-1"
environment        = "prod"
vpc_id             = "vpc-0123456789abcdef0"
public_subnet_ids  = ["subnet-aaa", "subnet-bbb"]
private_subnet_ids = ["subnet-ccc", "subnet-ddd"]

# From step 1:
dns_zone_id         = "Z0123456789ABCDEFG"
dns_name            = "cam.latheos.dev"
acm_certificate_arn = "arn:aws:acm:us-east-1:<acct>:certificate/<uuid>"

# From step 4 (can be a placeholder for the very first apply — Terraform
# won't launch containers until this resolves):
image_uri = "<acct>.dkr.ecr.us-east-1.amazonaws.com/cam-proxy:bootstrap"
```

After a clean apply you should see:

```
Outputs:
  public_hostname = "cam.latheos.dev"
  alb_dns_name    = "cam-proxy-prod-...elb.amazonaws.com"
```

The ALB is alive but the EC2 instances won't come healthy until we push an
image in the next step.

---

## 4. Build and push the container image — 5 min

Log your local Docker into ECR (the repo was created by Terraform):

```bash
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin \
      "$(aws sts get-caller-identity --query Account --output text).dkr.ecr.us-east-1.amazonaws.com"

cd ../..            # back to CAM_Cloud_Proxy repo root
make docker         # -> cam-proxy:dev
docker tag cam-proxy:dev "<acct>.dkr.ecr.us-east-1.amazonaws.com/cam-proxy:latest"
docker push          "<acct>.dkr.ecr.us-east-1.amazonaws.com/cam-proxy:latest"
```

After this, `terraform apply` once more with `image_uri` set to the real tag.
The ASG's instance refresh will roll the new image within a few minutes.

**After first deploy**, CI (`.github/workflows/ci.yml`) will build and push
every new `main` commit for you via OIDC. Set repo variable
`AWS_ROLE_TO_ASSUME` to the ARN of `cam-gha-ecr-push` (created by
`infra/terraform/ecr.tf`).

---

## 5. Populate vendor secrets in SSM — 2 min

The app reads its keys out of SSM Parameter Store at boot. Write each one
once as a `SecureString`:

```bash
aws ssm put-parameter --name "/cam/prod/deepgram_api_key" \
    --value "<paste>" --type SecureString --overwrite
aws ssm put-parameter --name "/cam/prod/groq_api_key"     \
    --value "<paste>" --type SecureString --overwrite
aws ssm put-parameter --name "/cam/prod/cartesia_api_key" \
    --value "<paste>" --type SecureString --overwrite
aws ssm put-parameter --name "/cam/prod/cartesia_voice_id" \
    --value "<paste>" --type String       --overwrite

# Optional — only if you picked xAI over Groq:
aws ssm put-parameter --name "/cam/prod/xai_api_key" \
    --value "<paste>" --type SecureString --overwrite
```

The EC2 user-data script pulls these into `/etc/cam/env` on boot; no app
restart needed beyond a single SSM SendCommand if you rotate a key later.

---

## 6. Provision your first hardware token — 30 seconds

```bash
cd CAM_Cloud_Proxy
make tokens-init                                           # idempotent
make tokens-provision USER=hamin TIER=standard QUOTA=600   # prints the token
# -> 8f3a2e1c9b7d4a5e6f0c2b8d1a3e4f67
```

That 32-char string is the *only* thing that links a physical drive to your
AWS account. Copy it into a note; you'll paste it into the installer in
step 9.

Sanity-check the proxy is answering:

```bash
curl -s https://cam.latheos.dev/healthz
# -> {"status":"ok","version":"0.1.0"}
```

If you get a cert error, DNS hasn't propagated yet — wait 5 min and retry.

---

## 7. Grab the LatheOS installer ISO — 2 min (or 20 if you build locally)

**The easy path — download the CI-built ISO.** Every green push to `main`
on `haminxx/LatheOS` publishes a 1.3 GB ISO as a workflow artifact. Tag
pushes (`v*`) publish it as a permanent GitHub Release. To grab the most
recent:

1. Open <https://github.com/haminxx/LatheOS/actions/workflows/nix.yml>
2. Click the top green run.
3. Scroll to *Artifacts* → `latheos-installer-<sha>` → download.
4. Unzip to get `latheos-*.iso`.

**The local path** (Linux / WSL / macOS + nix):

```bash
git clone https://github.com/haminxx/LatheOS.git
cd LatheOS
./scripts/build-latheos-iso.sh
# -> result-latheos-iso/iso/latheos-*.iso  (~1.3 GB)
```

---

## 8. Flash the ISO to a USB — 2 min

- **Linux**: `./scripts/flash-usb.sh path/to/latheos-*.iso /dev/sdX`
  (the script refuses to write to a mounted disk and uses `pv` for progress)
- **Windows**: [Rufus](https://rufus.ie) → *DD image mode* → pick the ISO,
  pick the USB, write.
- **macOS**: `diskutil list` → `sudo dd if=latheos.iso of=/dev/rdiskN bs=4m`.

Verify by booting once: the USB should drop you at a TTY with the message
`Welcome to the LatheOS live installer. Run: sudo /etc/latheos/install.sh`.

---

## 9. Install LatheOS onto the target machine — 10 min

Boot the target from the USB, log in as `nixos` (no password), and run:

```bash
sudo /etc/latheos/install.sh
```

It asks three questions:

1. **Target disk** — `/dev/nvme0n1` on most modern laptops. *This wipes it.*
2. **Hostname** — anything; `lathe-01` is fine.
3. **Hardware token** — paste the 32-char string from step 6.

The installer then:

- Partitions the NVMe: 513 MiB ESP, 90% ext4 (LABEL `latheos`), remainder
  exFAT (LABEL `LATHE_ASSETS` — this partition is cross-platform so you can
  plug the drive into macOS/Windows to move big files).
- Writes `/persist/secrets/cam.env` containing your token (mode 0600).
- Clones the LatheOS flake into `/etc/nixos/latheos`.
- Runs `nixos-install --flake .#latheos-x86_64`.

Reboot, remove the USB, and LatheOS comes up on tty1 with Sway.

---

## 10. First-boot verification — 3 min

On the freshly booted LatheOS host:

```bash
# 1. Daemon is up and idle:
systemctl status cam-daemon
# -> Active: active (running); journal: {"event":"daemon.idle", ...}

# 2. Control socket responds:
camctl ping
# -> {"ok": true, "pong": true}

# 3. Fire a synthetic activation — bypasses the mic, exercises the WS
#    roundtrip end-to-end:
camctl activate --kind wake_word
# tail the log:
journalctl -fu cam-daemon
# expect: wake.fired → cloud.connected → transcript.final (if you speak)

# 4. Real mic test:
#    say "Hey CAM, list my files"
# expect: wake.fired kind=wake_word → server sends a command frame that the
#         executor runs (you'll see `ls` output in the journal).
```

If step 3 logs `cloud.error: unknown hardware token` you pasted wrong — DynamoDB
is case-sensitive. Re-run `make tokens-list` on the CAM side, verify, and put
the exact value into `/persist/secrets/cam.env` on the lathe machine.

---

## Pipeline at a glance

```
     Microphone (PipeWire, 16 kHz mono)
            │
            ▼
┌───────────────────────┐
│  LatheOS cam-daemon   │     [systemd hardened unit]
│  ┌─────────────────┐  │
│  │ Activator       │  │  ← Porcupine wake-word + aubio onset
│  └────┬────────────┘  │
│       │ Activation              ← "Hey CAM" / clap / camctl
│       ▼
│  ┌─────────────────┐  │
│  │ WS client       │◄─┼──── audio frames (binary)
│  └────┬────────────┘  │
└───────┼───────────────┘
        │ wss://cam.latheos.dev/ws/cam
        ▼
┌──────────────────────────────────┐
│  AWS ALB  →  EC2 ASG             │  [WAF rate-limits /ws/cam]
│  ┌────────────────────────────┐  │
│  │ CAM Cloud Proxy (FastAPI)  │  │
│  │                            │  │
│  │  1. verify HW token  ──────┼──┼──► DynamoDB
│  │  2. stream PCM       ──────┼──┼──► Deepgram STT
│  │  3. Transcript       ──────┼──┼──► Groq / xAI LLM
│  │  4. LLM output:            │  │
│  │       text    ──── TTS ────┼──┼──► Cartesia (binary audio down)
│  │       JSON command  ───────┼──┼──► back to daemon's executor
│  └────────────────────────────┘  │
└──────────────────────────────────┘
        ▲
        │ binary audio + text frames
        │
┌───────┼───────────────────┐
│  Speaker (PipeWire)       │
│  Executor (allowlisted)   │  ← `docker compose up`, `sway exec`,
│                           │    `cursor .`, `git push`, etc.
└───────────────────────────┘
```

## Day-two ops

| Task | Command |
|---|---|
| Rotate a vendor key | `aws ssm put-parameter --name ... --value ... --overwrite` → restart proxy (`aws ssm send-command ... 'systemctl restart cam-proxy'`) |
| Issue a token for a new drive | `make tokens-provision USER=... TIER=... QUOTA=...` |
| Revoke a lost drive | `python -m app.admin.tokens revoke <token>` |
| Inspect token usage | `python -m app.admin.tokens show <token>` |
| Roll the ASG to a new image | bump `image_uri` tag in tfvars → `terraform apply` |
| Update the OS on a drive | `sudo nixos-rebuild switch --flake github:haminxx/LatheOS#latheos-x86_64` |
| Tail proxy logs | `aws logs tail /ec2/cam-proxy-prod --follow` |
| Tail daemon logs | `journalctl -fu cam-daemon` |

## Cost sanity check (us-east-1, prod defaults)

| Resource | Monthly |
|---|---|
| 2× c7g.large EC2 (24/7) | ~$50 |
| ALB (24/7 + traffic) | ~$18 |
| NAT gateway (1) | ~$32 |
| DynamoDB (pay-per-request, tokens only) | < $1 |
| Route53 hosted zone | $0.50 |
| WAF | ~$5 |
| **Total** | **~$110** |

Vendor costs (Deepgram/Groq/Cartesia) scale with use; at 1 hour/day of
conversation, expect ~$20/month combined. Put a billing alarm at $150 and
sleep well.

## Troubleshooting

- **`cloud.error: unknown hardware token`** → token mismatch. See step 10.
- **`wake.fired` never appears** → `PICOVOICE_ACCESS_KEY` not set in
  `/persist/secrets/cam.env`; without it the daemon falls back to
  control-socket-only mode (`camctl activate` still works).
- **ALB 503** → EC2 instances not healthy. `aws elbv2 describe-target-health`
  to see why; usually image pull failed (check ECR permissions on the IAM
  role, or a typo in `image_uri`).
- **CI red on LatheOS** → the `flake / per-config eval` job isolates each
  config; the failing step name tells you exactly which one broke. Stderr
  spills into the run's public Summary tab via `scripts/ci-eval.sh`.
