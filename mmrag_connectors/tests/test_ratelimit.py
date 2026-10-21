"""Confluence rate limiting: fewer requests (CQL with bodies), Retry-After honoured, clear failure."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from mmrag_connectors import ConnectorError, get


@pytest.fixture(autouse=True)
def reset(atlassian):
    httpx.post(atlassian + "/sim/ratelimit")          # limiter off, counters reset
    httpx.post(atlassian + "/sim/no_cql", params={"on": False})
    yield
    httpx.post(atlassian + "/sim/ratelimit")


def conf(atlassian, **cfg):
    return get("confluence")({"base_url": atlassian + "/confluence", "token": "sim-token", "max_rps": 50, **cfg})


def stats(atlassian):
    return httpx.get(atlassian + "/sim/stats").json()


def test_subtree_uses_a_handful_of_requests(atlassian):
    docs = list(conf(atlassian, roots="1000", depth=3).fetch())
    assert len(docs) == 17
    assert stats(atlassian)["requests"] <= 3, stats(atlassian)   # root + 1 CQL page (was ~34: page + children per page)


def test_depth_is_respected_with_cql(atlassian):
    assert len(list(conf(atlassian, roots="1000", depth=2).fetch())) == 5
    assert len(list(conf(atlassian, roots="1001", depth=2).fetch())) == 4


def test_space_in_one_request(atlassian):
    assert len(list(conf(atlassian, space="SIM").fetch())) == 17
    assert stats(atlassian)["requests"] == 1


def test_incremental_fetches_only_changed_pages(atlassian):
    since = datetime.now(timezone.utc) + timedelta(days=1, minutes=1)   # margin is 1 day -> nothing older
    httpx.post(atlassian + "/sim/touch/page/1005")
    c = conf(atlassian, roots="1000", depth=3)
    ids = [d.external_id for d in c.fetch(since=since - timedelta(minutes=5))]
    assert ids == ["confluence:1000", "confluence:1005"]  # root is always checked (hash-deduped later)


def test_falls_back_to_child_walk_without_cql(atlassian):
    httpx.post(atlassian + "/sim/no_cql", params={"on": True})
    assert len(list(conf(atlassian, roots="1000", depth=3).fetch())) == 17


def test_slows_down_before_the_bucket_is_empty(atlassian):
    httpx.post(atlassian + "/sim/ratelimit", params={"capacity": 2, "fill": 2.0})
    c = conf(atlassian, roots="1000\n1001\n1002\n1003", depth=1)   # 4+ requests, bucket of 2
    assert len(list(c.fetch())) == 4
    assert stats(atlassian)["limited"] == 0      # X-RateLimit-Remaining read -> paused instead of hitting 429


def test_429_retry_after_is_honoured(atlassian):
    # someone else already drained this user's bucket: the first requests get 429 + Retry-After
    httpx.post(atlassian + "/sim/ratelimit", params={"capacity": 2, "fill": 1.0, "start": 0})
    c = conf(atlassian, roots="1000\n1001\n1002", depth=1)
    assert len(list(c.fetch())) == 3
    assert c.http.throttled >= 1 and stats(atlassian)["limited"] >= 1


def test_paced_client_avoids_429(atlassian):
    httpx.post(atlassian + "/sim/ratelimit", params={"capacity": 2, "fill": 3.0})
    c = get("confluence")({"base_url": atlassian + "/confluence", "token": "sim-token", "max_rps": 2,
                           "roots": "1000\n1001\n1002\n1003\n1004", "depth": 1})
    assert len(list(c.fetch())) == 5
    assert stats(atlassian)["limited"] == 0          # max_rps below the fill rate -> never throttled


def test_persistent_rate_limit_gives_clear_error(atlassian):
    httpx.post(atlassian + "/sim/ratelimit", params={"capacity": 1, "fill": 0.001})
    c = conf(atlassian, roots="1000\n1001", depth=1)
    c.http.max_retries, c.http.max_wait = 1, 0.2
    with pytest.raises(ConnectorError) as e:
        list(c.fetch())
    assert "rate-limiting" in str(e.value) and "exempt" in str(e.value)
