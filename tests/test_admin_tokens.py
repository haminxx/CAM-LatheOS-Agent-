"""Unit test for the `app.admin.tokens` CLI.

We don't hit AWS — instead we prove that every documented subcommand
parses cleanly and dispatches to the right handler. That's enough to
catch the usual regressions (renamed argument, typo in subparser name)
without dragging moto or real IAM into the test suite.
"""

from __future__ import annotations

import pytest

from app.admin import tokens


def test_parser_accepts_every_documented_command() -> None:
    parser = tokens._build_parser()

    cases = [
        ["init-table"],
        ["provision", "--user-id", "alice", "--tier", "standard", "--quota", "600"],
        ["show", "deadbeef"],
        ["list", "--limit", "25"],
        ["topup", "deadbeef", "--minutes", "300"],
        ["revoke", "deadbeef"],
        ["delete", "deadbeef"],
    ]
    for argv in cases:
        ns = parser.parse_args(argv)
        assert ns.cmd in tokens._DISPATCH


def test_provision_requires_user_id() -> None:
    parser = tokens._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["provision"])


def test_tier_choices_are_enforced() -> None:
    parser = tokens._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["provision", "--user-id", "alice", "--tier", "platinum"])
