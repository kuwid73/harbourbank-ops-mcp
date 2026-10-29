import json

import pytest

from harbourbank_ops_mcp.safety import MAX_RESULT_CHARS, AuditLog, bound, guarded, mask, mask_text


@pytest.mark.parametrize("raw, masked", [
    ("customer ada.lovelace.3@sim.harbourbank.example", "customer a***@sim.harbourbank.example"),
    ("from HB1234567890 to HB0000004321", "from HB******7890 to HB******4321"),
    ("token ghp_abcdefghijklmnopqrstuvwxyz0123", "token [redacted]"),
    ("pat github_pat_11ABCDEFG0123456789_abcdefghij", "pat [redacted]"),
    ("Authorization: Bearer abc.def-ghi_jkl012345", "Authorization: Bearer [redacted]"),
    ("postgresql://bank:s3cr3t!@accounts-db-rw:5432/bank", "postgresql://bank:[redacted]@accounts-db-rw:5432/bank"),
    ("jwt eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJrdWJlcm5ldGVzIn0.c2lnbmF0dXJlLXZhbHVl", "jwt [redacted]"),
])
def test_mask_text(raw, masked):
    assert mask_text(raw) == masked


def test_mask_reaches_nested_values_and_keys():
    result = mask({"a@b.example": [{"line": "paid HB1234567890"}], "n": 3})
    assert result == {"a***@b.example": [{"line": "paid HB******7890"}], "n": 3}


def test_ordinary_text_is_left_alone():
    text = "GET /accounts/{account_id} 200 in 12.3 ms, sha-493df0c, 172.19.0.4"
    assert mask_text(text) == text


def test_bound_truncates_oversized_results():
    big = {"lines": ["x" * 1000] * 100}
    result = bound(big)
    assert result["truncated"] is True
    assert len(result["partial_json"]) == MAX_RESULT_CHARS
    assert bound({"ok": 1}) == {"ok": 1}


def test_guarded_masks_audits_and_turns_errors_into_results(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")

    @guarded(audit)
    def leaky(env: str) -> dict:
        return {"line": "owner bob@example.com"}

    @guarded(audit)
    def broken(account: str) -> dict:
        raise RuntimeError(f"lookup of {account} failed")

    assert leaky(env="prod") == {"line": "owner b***@example.com"}
    assert broken(account="HB1234567890") == {"error": "RuntimeError: lookup of HB******7890 failed"}

    entries = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [(e["tool"], e["outcome"]) for e in entries] == [("leaky", "ok"), ("broken", "error")]
    assert entries[1]["arguments"] == {"account": "HB******7890"}   # the audit log is masked too
    assert all({"ts", "duration_ms", "result_chars", "truncated"} <= e.keys() for e in entries)
