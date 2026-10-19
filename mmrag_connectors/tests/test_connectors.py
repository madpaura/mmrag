import time

import pytest

from mmrag_connectors import ConnectorError, LightRAGClient, LightRAGError, available, get


def test_registry():
    assert {"confluence", "jira"} <= set(available())


def test_validation_requires_fields():
    with pytest.raises(ConnectorError):
        get("jira")({"base_url": "http://x"})


def test_confluence_tree_and_depth(atlassian):
    c = get("confluence")({"base_url": atlassian + "/confluence", "token": "sim-token",
                           "roots": atlassian + "/confluence/spaces/SIM/pages/1000/Home", "depth": 2})
    assert "ok" in c.test()
    docs = list(c.fetch())
    assert len(docs) == 5  # root + 4 children
    assert docs[0].content.startswith("# Simulated Home")
    assert "| retry |" in docs[0].content
    assert len({d.file_source for d in docs}) == 5


def test_confluence_space_and_jira_follow(atlassian):
    c = get("confluence")({"base_url": atlassian + "/confluence", "token": "sim-token", "space": "SIM",
                           "follow_jira": True, "jira_instances": f"{atlassian}/jira|sim-token"})
    ids = [d.external_id for d in c.fetch()]
    assert sum(i.startswith("confluence:") for i in ids) == 17


def test_bad_token(atlassian):
    c = get("jira")({"base_url": atlassian + "/jira", "token": "nope"})
    with pytest.raises(ConnectorError):
        list(c.fetch())


def test_jira_project_links_and_incremental(atlassian):
    j = get("jira")({"base_url": atlassian + "/jira", "token": "sim-token", "project": "SIM"})
    docs = list(j.fetch())
    assert len(docs) == 12 and "## Comments" in docs[0].content
    j2 = get("jira")({"base_url": atlassian + "/jira", "token": "sim-token", "mode": "jql", "jql": "key = SIM-3", "link_depth": 1})
    assert {d.title.split(":")[0] for d in j2.fetch()} == {"SIM-3", "SIM-4"}


def test_lightrag_roundtrip(lightrag_url):
    c = LightRAGClient(lightrag_url)
    t = c.insert_text("hello world", "a.md")
    with pytest.raises(LightRAGError) as e:
        c.insert_text("again", "a.md")
    assert e.value.status == 409
    res = c.wait([t], interval=0.1, timeout=10)
    doc = res[t]["documents"][0]
    assert doc["status"] == "PROCESSED"
    c.delete_documents([doc["id"]])
    end = time.time() + 5
    while c.status_counts() and time.time() < end:  # deletion runs in the background
        time.sleep(0.1)
    assert c.status_counts() == {}


def test_lightrag_busy_refusal(lightrag_url):
    from mmrag_connectors import LightRAGBusy
    c = LightRAGClient(lightrag_url)
    c.insert_text("x", "b.md")
    with pytest.raises(LightRAGBusy):
        c.clear()


def test_confluence_bad_token_fails_instead_of_skipping(atlassian):
    c = get("confluence")({"base_url": atlassian + "/confluence", "token": "nope", "roots": "1000"})
    with pytest.raises(ConnectorError):
        list(c.fetch())


def test_confluence_missing_page_is_skipped(atlassian):
    c = get("confluence")({"base_url": atlassian + "/confluence", "token": "sim-token", "roots": "1000\n424242", "depth": 1})
    assert [d.external_id for d in c.fetch()] == ["confluence:1000"]


def test_upsert_replaces_existing_even_while_busy(lightrag_url):
    import mock_lightrag
    c = LightRAGClient(lightrag_url)
    c.insert_text("v1", "doc.md")                        # pipeline busy for a moment
    t = c.upsert_text("v2", "doc.md", interval=0.1, timeout=20)
    c.wait([t], interval=0.1, timeout=10)
    assert [d["text"] for d in mock_lightrag.DOCS.values()] == ["v2"]
