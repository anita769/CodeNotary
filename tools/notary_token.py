#!/usr/bin/env python3
"""Minimal capability tokens for the audited write channel (RFC 7519 HS256).

A token carries CLAIMS, not power: subject / role / scope / credential_id
/ exp. The audit trail records the claims — never the raw token.

  mint:    python3 tools/notary_token.py mint --sub chen --role adjudicator \
               --scope adjudicate --ttl 1800 --secret-file keys/token_secret
  verify:  python3 tools/notary_token.py verify <token> --secret-file ...

Pure stdlib (hmac + hashlib + base64). HS256 is a symmetric gateway-secret
scheme by design: this is a single-issuer internal capability token, not a
federation format.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import secrets as _secrets
import sys
import time
from pathlib import Path


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def mint(claims: dict, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    head = _b64e(json.dumps(header, separators=(",", ":")).encode())
    body = _b64e(json.dumps(claims, ensure_ascii=False,
                            separators=(",", ":")).encode())
    sig = _b64e(hmac.new(secret.encode(), f"{head}.{body}".encode(),
                         hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


def verify(token: str, secret: str, now: float | None = None) -> dict:
    """Return claims or raise ValueError with a reason."""
    try:
        head, body, sig = token.split(".")
    except ValueError:
        raise ValueError("malformed token (expected 3 segments)")
    expected = _b64e(hmac.new(secret.encode(), f"{head}.{body}".encode(),
                              hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise ValueError("bad signature")
    claims = json.loads(_b64d(body).decode("utf-8"))
    now = time.time() if now is None else now
    if float(claims.get("exp", 0)) < now:
        raise ValueError("token expired")
    for field in ("sub", "role", "credential_id"):
        if not claims.get(field):
            raise ValueError(f"missing claim {field}")
    return claims


def redacted_claims(claims: dict) -> dict:
    """What the audit trail may store — claims, NEVER the token itself."""
    return {"subject": claims.get("sub"), "role": claims.get("role"),
            "scope": claims.get("scope", []),
            "credential_id": claims.get("credential_id"),
            "jti": claims.get("jti")}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("mint", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--secret-file", default="keys/token_secret")
        if name == "mint":
            p.add_argument("--sub", required=True)
            p.add_argument("--role", required=True)
            p.add_argument("--scope", required=True,
                           help="comma-separated, e.g. adjudicate,evidence:read")
            p.add_argument("--ttl", type=int, default=1800)
            p.add_argument("--credential-id", default=None)
        else:
            p.add_argument("token")
    args = ap.parse_args()
    secret_path = Path(args.secret_file)
    if not secret_path.exists():
        if args.cmd == "mint":
            secret_path.parent.mkdir(parents=True, exist_ok=True)
            secret_path.write_text(_secrets.token_urlsafe(32) + "\n")
            print(f"generated new secret -> {secret_path} (keep it private)",
                  file=sys.stderr)
        else:
            raise SystemExit(f"secret file {secret_path} not found")
    secret = secret_path.read_text().strip()

    if args.cmd == "mint":
        claims = {"sub": args.sub, "role": args.role,
                  "scope": sorted(s.strip() for s in args.scope.split(",")
                                  if s.strip()),
                  "credential_id": args.credential_id
                                   or f"cred-{_secrets.token_hex(4)}",
                  "iat": int(time.time()), "exp": int(time.time()) + args.ttl,
                  "jti": _secrets.token_hex(8)}
        print(mint(claims, secret))
        print(json.dumps(redacted_claims(claims), ensure_ascii=False),
              file=sys.stderr)
    else:
        try:
            claims = verify(args.token, secret)
        except ValueError as exc:
            raise SystemExit(f"INVALID: {exc}")
        print(json.dumps(redacted_claims(claims), ensure_ascii=False,
                         indent=2))


if __name__ == "__main__":
    main()
