# CAM Cloud Proxy

AWS-hosted FastAPI orchestrator for **LatheOS**. Bridges local wake-word
events to Deepgram (STT), Groq/xAI (reasoning), and Cartesia (TTS) over a
single persistent WebSocket — optimised for sub-second round-trips.

> **First time here?** Read [`SETUP.md`](./SETUP.md) for the zero-to-working
> runbook (AWS account, vendor keys, Terraform, first hardware token, ISO
> flash, verification). Everything below this line is reference material
> you'll want *after* that walkthrough.

## Quick start (local dev, mock vendors)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Leave vendor keys blank — mock clients will activate automatically.
# Enable the dev auth bypass:
echo "ALLOW_UNVERIFIED_TOKENS=true" >> .env
echo "ENV=dev" >> .env

python -m app.main
```

Connect a client to `ws://localhost:8080/ws/cam`. Send a `hello` JSON frame,
then stream PCM s16le @ 16 kHz as binary frames.

## Wire protocol

| Frame     | Direction | Payload                                             |
|-----------|-----------|-----------------------------------------------------|
| text      | C → S     | `Hello` (JSON), first frame only                    |
| binary    | C → S     | Raw PCM audio chunks                                |
| text      | S → C     | `Transcript` / `Command` / `SpeechStart/End` / `Error` |
| binary    | S → C     | Cartesia PCM chunks (between SpeechStart and SpeechEnd) |

See `app/schemas/messages.py` for the authoritative contract.

## Deploy to EC2

1. Push image: `docker build -t $ECR/cam-proxy:latest . && docker push ...`
2. Populate SSM `/cam/prod/*` with vendor keys.
3. Launch EC2 instance with `infra/ec2-userdata.sh` and `IMAGE_URI` set.
4. Front with ALB over TLS — WebSockets only terminate once per node.

## Hardware-token administration

Every LatheOS NVMe needs a row in the `CAM_HardwareTokens` DynamoDB table
before its daemon can open the WebSocket. The admin CLI is the only
supported way to write to that table:

```bash
# One-time (idempotent) table creation:
make tokens-init

# Issue a new token; prints the 32-char token on stdout:
make tokens-provision USER=hamin TIER=standard QUOTA=600

# Inspect / manage:
python -m app.admin.tokens show   <token>
python -m app.admin.tokens list   --limit 50
python -m app.admin.tokens topup  <token> --minutes 600
python -m app.admin.tokens revoke <token>
python -m app.admin.tokens delete <token>
```

The CLI uses the ambient AWS credentials (`AWS_PROFILE` / IAM role), so every
write is attributable in CloudTrail. Paste the printed token straight into
`/persist/secrets/cam.env` on the target drive.

## Architecture

```
LatheOS daemon
      │  (TLS WebSocket)
      ▼
FastAPI /ws/cam ──► DynamoDB token check
      │
      ▼
CamSession ── audio ──► Deepgram STT ──► LLMRouter (Groq/xAI)
                                              │
                 ┌────────────────────────────┼─────────────────────┐
                 ▼                            ▼                     ▼
           Cartesia TTS                  Command JSON          Transcript
         (binary audio)                (text frame)          (text frame)
```
