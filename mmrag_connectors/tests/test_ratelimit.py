"""Confluence page selection and rate limiting: few requests, depth honoured for roots and spaces, slow answers
retried in smaller batches, Retry-After honoured, clear failure."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from mmrag_connectors import ConnectorError, get


@pytest.fixture(autouse=True)
def reset(atlassian):
    httpx.post(atlassian + "/sim/ratelimit")          # limiter off, counters reset
    httpx.post(atlassian + "/sim/reset")              # initial pages (other tests touch them), CQL on, not slow
    yield
    httpx.post(atlassian + "/sim/reset")
    httpx.post(atlassian + "/sim/ratelimit")


def conf(atlassian, **cfg):
    return get("confluence")({"base_url": atlassian + "/confluence", "token": "sim-token", "max_rps": 50, **cfg})


def stats(atlassian):
    return httpx.get(atlassian + "/sim/stats").json()


def test_subtree_uses_a_handful_of_requests(atlassian):
    assert len(list(conf(atlassian, roots="1000", depth=0).fetch())) == 17
    assert stats(atlassian)["requests"] == 3, stats(atlassian)   # root, one CQL listing, one batch of bodies


def test_limited_depth_reads_only_the_levels_it_needs(atlassian):
    """A shallow selection under a big tree must not list (or render) the levels below it."""
    assert len(list(conf(atlassian, roots="1000", depth=2).fetch())) == 5
    assert stats(atlassian)["requests"] == 3, stats(atlassian)   # root, its children, one batch of bodies
    httpx.post(atlassian + "/sim/ratelimit")
    assert len(list(conf(atlassian, roots="1000", depth=3).fetch())) == 17
    assert stats(atlassian)["requests"] == 7, stats(atlassian)   # + children of the 4 level-1 pages


def test_wide_level_switches_to_one_search(atlassian):
    c = conf(atlassian, roots="1000", depth=3)
    c.walk_max = 2                                               # level 1 has 4 pages: one CQL search instead of 4
    assert len(list(c.fetch())) == 17
    assert stats(atlassian)["requests"] == 4, stats(atlassian)


def test_space_depth_starts_at_its_top_level_pages(atlassian):
    assert [d.external_id for d in conf(atlassian, space="SIM", depth=1).fetch()] == ["confluence:1000"]
    assert len(list(conf(atlassian, space="SIM", depth=2).fetch())) == 5
    assert len(list(conf(atlassian, space="SIM").fetch())) == 17   # default depth 3 covers the simulated tree


def test_unknown_space_is_a_clear_error(atlassian):
    with pytest.raises(ConnectorError, match="space 'NOPE' not found"):
        list(conf(atlassian, space="NOPE", depth=2).fetch())


def test_depth_is_respected_with_cql(atlassian):
    assert len(list(conf(atlassian, roots="1000", depth=2).fetch())) == 5
    assert len(list(conf(atlassian, roots="1001", depth=2).fetch())) == 4


def test_whole_space_in_two_requests(atlassian):
    assert len(list(conf(atlassian, space="SIM", depth=0).fetch())) == 17
    assert stats(atlassian)["requests"] == 2                      # one CQL listing, one batch of bodies


def test_incremental_fetches_only_changed_pages(atlassian):
    since = datetime.now(timezone.utc) + timedelta(days=1, minutes=1)   # margin is 1 day -> nothing older
    httpx.post(atlassian + "/sim/touch/page/1005")
    c = conf(atlassian, roots="1000", depth=3)
    ids = [d.external_id for d in c.fetch(since=since - timedelta(minutes=5))]
    assert ids == ["confluence:1005"]                     # unchanged pages (root included) are not downloaded


def test_incremental_without_cql_filters_by_version_date(atlassian):
    httpx.post(atlassian + "/sim/no_cql", params={"on": True})
    httpx.post(atlassian + "/sim/touch/page/1005")
    since = datetime.now(timezone.utc) + timedelta(days=1) - timedelta(minutes=5)
    assert [d.external_id for d in conf(atlassian, roots="1000", depth=3).fetch(since=since)] == ["confluence:1005"]


def test_slow_answers_are_retried_in_smaller_batches(atlassian):
    httpx.post(atlassian + "/sim/slow", params={"body": 0.15})   # 17 bodies take 2.5 s, the timeout is 1 s
    logs = []
    c = get("confluence")({"base_url": atlassian + "/confluence", "token": "sim-token", "max_rps": 50, "timeout": 1,
                           "roots": "1000", "depth": 0}, logs.append)
    assert len(list(c.fetch())) == 17
    assert c._limits["bodies"] <= 4 and any("too slowly" in m for m in logs)


def test_one_page_too_slow_on_its_own_is_skipped(atlassian):
    httpx.post(atlassian + "/sim/slow", params={"page": "1005", "seconds": 3})
    logs = []
    c = get("confluence")({"base_url": atlassian + "/confluence", "token": "sim-token", "max_rps": 50, "timeout": 1,
                           "roots": "1001", "depth": 2}, logs.append)
    assert [d.external_id for d in c.fetch()] == ["confluence:1001", "confluence:1006", "confluence:1007"]
    assert any("skip page 1005" in m and "did not answer within 1 s" in m for m in logs)


def test_a_struggling_server_fails_the_run(atlassian):
    httpx.post(atlassian + "/sim/slow", params={"body": 2})
    c = get("confluence")({"base_url": atlassian + "/confluence", "token": "sim-token", "max_rps": 50, "timeout": 1,
                           "roots": "1001", "depth": 2})
    with pytest.raises(ConnectorError, match="did not answer within 1 s"):
        list(c.fetch())


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
