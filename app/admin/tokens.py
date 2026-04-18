"""Hardware-token admin CLI.

Closes the auth loop: every LatheOS NVMe needs a row in the
`CAM_HardwareTokens` DynamoDB table before it can talk to the proxy. This
CLI is the only supported way to write that row.

Kept intentionally boring — no framework, just boto3 + argparse — so it
runs from a laptop, a CI job, or a shell-in to an EC2 admin bastion
without dragging FastAPI along for the ride.

Usage
-----

    # One-time table creation (idempotent):
    python -m app.admin.tokens init-table

    # Provision a new token for a user:
    python -m app.admin.tokens provision \\
        --user-id hamin --tier standard --quota 600
    # -> prints the 32-char token to stdout

    # Inspect:
    python -m app.admin.tokens show <token>
    python -m app.admin.tokens list --limit 50

    # Lifecycle:
    python -m app.admin.tokens topup   <token> --minutes 600
    python -m app.admin.tokens revoke  <token>
    python -m app.admin.tokens delete  <token>

Every write op is logged with the caller's AWS identity so provisioning
leaves an audit trail in CloudTrail.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import UTC, datetime

import boto3
from botocore.exceptions import ClientError

from app.config import get_settings

TOKEN_LEN = 32  # hex characters — 128 bits of entropy, fits on a sticker


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _table():
    s = get_settings()
    return boto3.resource("dynamodb", region_name=s.aws_region).Table(s.dynamodb_table)


def _client():
    s = get_settings()
    return boto3.client("dynamodb", region_name=s.aws_region)


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, default=str, sort_keys=True))


def _coerce(item: dict) -> dict:
    """Normalise DynamoDB's Decimal values to plain ints for display."""
    out: dict = {}
    for k, v in item.items():
        out[k] = int(v) if hasattr(v, "to_integral_value") else v
    return out


# ---------------------------------------------------------------------------
# ops
# ---------------------------------------------------------------------------


def cmd_init_table(_args: argparse.Namespace) -> int:
    s = get_settings()
    c = _client()
    try:
        c.describe_table(TableName=s.dynamodb_table)
        print(f"table '{s.dynamodb_table}' already exists in {s.aws_region}", file=sys.stderr)
        return 0
    except c.exceptions.ResourceNotFoundException:
        pass

    c.create_table(
        TableName=s.dynamodb_table,
        AttributeDefinitions=[{"AttributeName": "token", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "token", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
        SSESpecification={"Enabled": True},
    )
    c.get_waiter("table_exists").wait(TableName=s.dynamodb_table)
    c.update_continuous_backups(
        TableName=s.dynamodb_table,
        PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
    )
    print(f"created table '{s.dynamodb_table}' in {s.aws_region}", file=sys.stderr)
    return 0


def cmd_provision(args: argparse.Namespace) -> int:
    token = secrets.token_hex(TOKEN_LEN // 2)  # 32 hex chars
    item = {
        "token": token,
        "user_id": args.user_id,
        "tier": args.tier,
        "quota_remaining": int(args.quota),
        "created_at": _now(),
        "revoked": False,
    }
    # Condition prevents accidentally clobbering an existing token on a
    # freak collision — astronomically unlikely but the guard is free.
    try:
        _table().put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(#t)",
            ExpressionAttributeNames={"#t": "token"},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            print("token collision — try again", file=sys.stderr)
            return 2
        raise

    if args.json:
        _print_json(item)
    else:
        # Flat output is easier to paste into /persist/secrets/cam.env.
        print(token)
        print(f"user_id={args.user_id}  tier={args.tier}  quota={args.quota}", file=sys.stderr)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    resp = _table().get_item(Key={"token": args.token})
    item = resp.get("Item")
    if not item:
        print("not found", file=sys.stderr)
        return 1
    _print_json(_coerce(item))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    # Full scan: acceptable for an admin tool, never called from the hot path.
    paginator = _client().get_paginator("scan")
    pages = paginator.paginate(TableName=get_settings().dynamodb_table, Limit=args.limit)
    rows: list[dict] = []
    for page in pages:
        for raw in page.get("Items", []):
            rows.append(
                {
                    "token": raw["token"]["S"],
                    "user_id": raw.get("user_id", {}).get("S", "-"),
                    "tier": raw.get("tier", {}).get("S", "-"),
                    "quota_remaining": int(raw.get("quota_remaining", {}).get("N", "0")),
                    "revoked": raw.get("revoked", {}).get("BOOL", False),
                }
            )
            if len(rows) >= args.limit:
                break
        if len(rows) >= args.limit:
            break

    if args.json:
        _print_json(rows)
        return 0

    if not rows:
        print("(no tokens)", file=sys.stderr)
        return 0
    hdr = f"{'TOKEN':<34}  {'USER':<16}  {'TIER':<10}  {'QUOTA':>6}  REVOKED"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        tok = r["token"][:8] + "…" + r["token"][-4:]
        print(
            f"{tok:<34}  {r['user_id']:<16}  {r['tier']:<10}  {r['quota_remaining']:>6}  {r['revoked']}"
        )
    return 0


def cmd_topup(args: argparse.Namespace) -> int:
    resp = _table().update_item(
        Key={"token": args.token},
        UpdateExpression="SET quota_remaining = if_not_exists(quota_remaining, :z) + :q",
        ExpressionAttributeValues={":q": int(args.minutes), ":z": 0},
        ConditionExpression="attribute_exists(#t)",
        ExpressionAttributeNames={"#t": "token"},
        ReturnValues="ALL_NEW",
    )
    _print_json(_coerce(resp["Attributes"]))
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    _table().update_item(
        Key={"token": args.token},
        UpdateExpression="SET revoked = :r, quota_remaining = :z",
        ExpressionAttributeValues={":r": True, ":z": 0},
        ConditionExpression="attribute_exists(#t)",
        ExpressionAttributeNames={"#t": "token"},
    )
    print(f"revoked {args.token}", file=sys.stderr)
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    _table().delete_item(
        Key={"token": args.token},
        ConditionExpression="attribute_exists(#t)",
        ExpressionAttributeNames={"#t": "token"},
    )
    print(f"deleted {args.token}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.admin.tokens",
        description="CAM hardware-token administration.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-table", help="create the DynamoDB table if missing")

    pr = sub.add_parser("provision", help="issue a new hardware token")
    pr.add_argument("--user-id", required=True)
    pr.add_argument("--tier", choices=["dev", "standard", "pro"], default="standard")
    pr.add_argument("--quota", type=int, default=600, help="minutes of audio")
    pr.add_argument("--json", action="store_true")

    sh = sub.add_parser("show", help="print one token's record")
    sh.add_argument("token")

    ls = sub.add_parser("list", help="list tokens (admin only — full scan)")
    ls.add_argument("--limit", type=int, default=50)
    ls.add_argument("--json", action="store_true")

    tu = sub.add_parser("topup", help="add minutes to a token's quota")
    tu.add_argument("token")
    tu.add_argument("--minutes", type=int, required=True)

    rv = sub.add_parser("revoke", help="mark a token revoked and zero its quota")
    rv.add_argument("token")

    dl = sub.add_parser("delete", help="hard-delete a token row")
    dl.add_argument("token")

    return p


_DISPATCH = {
    "init-table": cmd_init_table,
    "provision": cmd_provision,
    "show": cmd_show,
    "list": cmd_list,
    "topup": cmd_topup,
    "revoke": cmd_revoke,
    "delete": cmd_delete,
}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return _DISPATCH[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
