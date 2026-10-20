"""TLS trust for corporate networks.

Internal Confluence/Jira servers usually present certificates issued by a company CA that public trust stores
(certifi) don't know -> "certificate verify failed: unable to get local issuer certificate".

`verify_for(True)` returns an SSL context that trusts the public CAs *plus* any extra CA certificates from:
  MMRAG_CA_BUNDLE   a PEM file (one or more certificates)
  MMRAG_CA_DIR      a directory of *.crt / *.pem files   (the portal container mounts config/ca here)
  SSL_CERT_FILE     the conventional variable, honoured as well
Partial chains are accepted, so trusting the company's intermediate CA is enough even when the server does not
send (and you do not have) the root.
"""
from __future__ import annotations

import os
import ssl
from pathlib import Path

try:
    import certifi
except ImportError:  # pragma: no cover - httpx depends on certifi
    certifi = None


def extra_ca_files() -> list[Path]:
    files: list[Path] = []
    for var in ("MMRAG_CA_BUNDLE", "SSL_CERT_FILE"):
        p = os.environ.get(var)
        if p and Path(p).is_file():
            files.append(Path(p))
    d = os.environ.get("MMRAG_CA_DIR")
    if d and Path(d).is_dir():
        files += sorted(f for f in Path(d).iterdir() if f.suffix.lower() in (".crt", ".pem") and f.is_file())
    return files


def verify_for(verify: bool = True) -> ssl.SSLContext | bool:
    """What to pass as httpx's `verify=`: False, or a context with public + company CAs."""
    if not verify:
        return False
    ctx = ssl.create_default_context(cafile=certifi.where()) if certifi else ssl.create_default_context()
    for f in extra_ca_files():
        try:
            ctx.load_verify_locations(cafile=str(f))
        except (ssl.SSLError, OSError):
            continue  # a broken file must not break all connections
    ctx.verify_flags |= getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    return ctx


def explain(exc: Exception, url: str = "") -> str:
    """Turn a certificate failure into an actionable message."""
    msg = str(exc)
    if "CERTIFICATE_VERIFY_FAILED" in msg or "certificate verify failed" in msg:
        target = url or "the server URL"
        import re
        reason = re.search(r"certificate verify failed:\s*([^(\]]+)", msg)
        return (f"TLS certificate not trusted ({reason.group(1).strip() if reason else 'verify failed'}). "
                f"The server uses a company CA: on the deploy host run  ./setup.sh trust-ca {target}  "
                f"(scripts: export MMRAG_CA_BUNDLE=<company-ca.pem>). Or untick 'Verify TLS certificates' (not recommended).")
    return msg
