"""Shared base for Atlassian Data Center connectors (Confluence, Jira).

Both products take the same connection settings (base URL, bearer personal access token, TLS, request pacing) and
fail the same ways. ``AtlassianConnector`` owns that part, so a product connector only describes *what* to ingest:

    @register
    class ConfluenceConnector(AtlassianConnector):
        product = "Confluence"
        config_schema = AtlassianConnector.schema([...product fields...], base_help="...")

        def check(self) -> str:              # cheap authenticated call, used by test()
            ...
        def fetch(self, since=None):         # yields Document, using self.get_json(path, **params)
            ...
"""
from __future__ import annotations

from typing import Any, ClassVar

import httpx

from . import tls
from .base import Connector, ConnectorError, Field_
from .ratelimit import RateLimitedClient


class SlowResponse(ConnectorError):
    """The server accepted the request but did not answer within the timeout."""


class AtlassianConnector(Connector):
    product: ClassVar[str] = ""            # "Confluence" / "Jira", used in messages
    rate_limit_admin: ClassVar[str] = ""   # where an admin exempts an account from rate limiting

    @classmethod
    def schema(cls, fields: list[Field_], base_help: str = "") -> list[Field_]:
        """Connection fields first, then the product's own fields, then request tuning (the form follows this order)."""
        return [
            Field_("base_url", "Base URL", required=True, help=base_help),
            Field_("token", "Personal access token", "password", required=True),
            *fields,
            Field_("max_rps", "Max requests per second", "number", default=2,
                   help=f"Stays under {cls.product}'s rate limit; 429 answers are waited out (Retry-After)."),
            Field_("timeout", "Request timeout (seconds)", "number", default=120,
                   help=f"How long to wait for one answer from {cls.product}. Slow answers are retried in smaller batches."),
            Field_("verify_ssl", "Verify TLS certificates", "bool", default=True,
                   help="Turn off only for self-signed internal servers."),
        ]

    def __init__(self, config: dict[str, Any], log=None):
        super().__init__(config, log)
        c = self.config
        self.base = c["base_url"].rstrip("/")
        self.http = RateLimitedClient(headers={"Authorization": f"Bearer {c['token']}"},
                                      verify=tls.verify_for(bool(c["verify_ssl"])),
                                      max_rps=float(c.get("max_rps") or 2), log=self.log,
                                      timeout=float(c.get("timeout") or 120))

    def get_json(self, path: str, **params) -> Any:
        """GET base_url+path. Access, connection and rate-limit failures raise ConnectorError (they fail the run);
        any other HTTP error raises httpx.HTTPStatusError, so callers can skip e.g. a deleted page. A response slower
        than the timeout raises SlowResponse (a ConnectorError), so callers can retry with less work per request."""
        try:
            r = self.http.get(self.base + path, params=params)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:  # TLS / DNS / refused: fail the run, don't skip
            raise ConnectorError(f"cannot reach {self.product}: {tls.explain(e, self.base)}")
        except httpx.TimeoutException:
            raise SlowResponse(f"{self.product} did not answer within {self.http.timeout:g} s ({path}). Raise "
                               f"'Request timeout', or select fewer pages.")
        except httpx.TransportError as e:
            raise ConnectorError(f"connection to {self.product} failed: {e or type(e).__name__}")
        if r.status_code in (401, 403):
            raise ConnectorError(f"{self.product} denied access ({r.status_code}) for {path}")
        if r.status_code in (429, 503):
            raise ConnectorError(f"{self.product} kept rate-limiting ({r.status_code}) after {self.http.max_retries} "
                                 f"retries. Lower 'Max requests per second', or ask the {self.product} admin to exempt "
                                 f"this account ({self.rate_limit_admin}).")
        r.raise_for_status()
        return r.json()

    def check(self) -> str:
        """One cheap authenticated call; return what to show after 'ok'."""
        raise NotImplementedError

    def test(self) -> str:
        try:
            return f"ok ({self.check()})"
        except httpx.HTTPError as e:
            raise ConnectorError(f"cannot reach {self.product}: {tls.explain(e, self.base)}")

    def close(self) -> None:
        self.http.close()
