"""End-to-end subject behaviour: harvest interleaving and the rules web API."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.oai import OaiClient
from conftest import wait_for_outcome


def catalog_item(store, identifier: str) -> dict | None:
    for item in store.catalog_page(page_size=200)["records"]:
        if item["identifier"] == identifier:
            return item
    return None


# --------------------------------------------------------------- harvest level
async def test_harvest_assigns_subjects_using_rules_in_force(harvester, store, repo):
    store.upsert_subject_rule("数据治理", "数据管理", 10)
    store.upsert_subject_rule("图书馆", "图书馆事业", 5)

    control = await harvester.start_run()
    assert await wait_for_outcome(control) == "succeeded"

    item = catalog_item(store, "oai:demo:0002")  # 高校图书馆数据治理实践
    assert item["subject"] == "数据管理"  # highest-priority winning rule
    # A title matching only the lower-priority keyword.
    item9 = catalog_item(store, "oai:demo:0009")  # 下一代图书馆服务平台的架构设计
    assert item9["subject"] == "图书馆事业"
    # No keyword hit -> subject stays empty.
    item8 = catalog_item(store, "oai:demo:0008")  # 古籍数字化中的图像检索
    assert item8["subject"] is None


async def test_highest_priority_conflict_fails_whole_round(harvester, store, repo):
    control = await harvester.start_run()
    assert await wait_for_outcome(control) == "succeeded"
    before = store.published_state()
    before_rows = store.catalog_page(page_size=200)["records"]

    await asyncio.sleep(1.05)
    repo.add_record(title="图书馆数据治理冲突论文")
    store.upsert_subject_rule("图书馆", "图书馆事业", 10)
    store.upsert_subject_rule("数据", "数据科学", 10)

    conflicting = await harvester.start_run()
    outcome = await wait_for_outcome(conflicting)
    assert outcome == "failed"
    run = store.get_run(conflicting.run_id)
    assert "冲突" in (run["last_error"] or "")

    # Catalogue, subjects and watermark are exactly as before.
    after = store.published_state()
    assert after["watermark"] == before["watermark"]
    assert after["success_count"] == before["success_count"]
    assert store.catalog_page(page_size=200)["records"] == before_rows


async def test_adjusting_rules_allows_reharvest_of_same_window(harvester, store, repo):
    control = await harvester.start_run()
    assert await wait_for_outcome(control) == "succeeded"
    baseline_watermark = store.published_state()["watermark"]

    await asyncio.sleep(1.05)
    repo.add_record(title="图书馆数据治理冲突论文")
    store.upsert_subject_rule("图书馆", "图书馆事业", 10)
    store.upsert_subject_rule("数据", "数据科学", 10)

    bad = await harvester.start_run()
    assert await wait_for_outcome(bad) == "failed"
    # The failed round advanced nothing.
    assert store.published_state()["watermark"] == baseline_watermark

    # Resolve the conflict: one rule now clearly outranks the other.
    store.upsert_subject_rule("数据", "数据科学", 20)
    good = await harvester.start_run()
    assert await wait_for_outcome(good) == "succeeded"

    assert store.published_state()["watermark"] > baseline_watermark
    titles = {
        row["identifier"]: row
        for row in store.catalog_page(page_size=200)["records"]
    }
    new_id = next(
        identifier
        for identifier in titles
        if identifier not in {f"oai:demo:{i:04d}" for i in range(1, 11)}
    )
    assert titles[new_id]["subject"] == "数据科学"


async def test_corrected_title_is_reclassified_and_subject_cleared(harvester, store, repo):
    store.upsert_subject_rule("数据治理", "数据管理", 10)
    first = await harvester.start_run()
    await wait_for_outcome(first)
    assert catalog_item(store, "oai:demo:0002")["subject"] == "数据管理"

    await asyncio.sleep(1.05)
    repo.touch_record("oai:demo:0002", title="更正后与规则无关的标题")
    second = await harvester.start_run()
    assert await wait_for_outcome(second) == "succeeded"
    assert catalog_item(store, "oai:demo:0002")["subject"] is None


async def test_withdrawal_removes_subject_with_row(harvester, store, repo):
    store.upsert_subject_rule("图像", "数字人文", 10)
    first = await harvester.start_run()
    await wait_for_outcome(first)
    assert catalog_item(store, "oai:demo:0008")["subject"] == "数字人文"

    await asyncio.sleep(1.05)
    repo.withdraw("oai:demo:0008")
    second = await harvester.start_run()
    assert await wait_for_outcome(second) == "succeeded"
    assert catalog_item(store, "oai:demo:0008") is None


async def test_rule_edit_between_harvests_changes_next_round_subjects(
    harvester, store, repo
):
    item_id = "oai:demo:0009"  # 下一代图书馆服务平台的架构设计
    store.upsert_subject_rule("服务平台", "旧主题", 10)
    first = await harvester.start_run()
    await wait_for_outcome(first)
    assert catalog_item(store, item_id)["subject"] == "旧主题"

    # Rule corrected before the next harvest; the correction arrives through
    # the source at the same time.  "架构设计" is unique to this seed title,
    # so no other record is affected by the replacement rule.
    store.delete_subject_rule(store.list_subject_rules()[0]["id"])
    store.upsert_subject_rule("架构设计", "图书馆事业", 10)
    await asyncio.sleep(1.05)
    repo.touch_record(item_id)
    second = await harvester.start_run()
    assert await wait_for_outcome(second) == "succeeded"
    assert catalog_item(store, item_id)["subject"] == "图书馆事业"


# ------------------------------------------------------------------ web level
@pytest.fixture
def web_app(tmp_path: Path, demo_app, repo):
    def factory() -> OaiClient:
        return OaiClient(
            "http://demo.local/oai",
            transport=httpx.ASGITransport(app=demo_app),
        )

    return create_app(
        db_path=tmp_path / "web-rules.sqlite3",
        oai_url="http://demo.local/oai",
        client_factory=factory,
    )


def test_rules_page_renders_and_catalog_api_carries_subject(web_app):
    with TestClient(web_app) as client:
        assert client.get("/rules").status_code == 200
        client.post("/api/harvest")
        _poll(client, lambda p: p["active"] is None and p["published"]["watermark"])

        client.post(
            "/api/rules",
            data={"keyword": "数据治理", "subject": "数据管理", "priority": "10"},
        )
        # Not confirmed yet: published subjects remain empty.
        page = client.get("/").text
        assert 'class="subject-tag"' not in page  # no subject tag rendered
        assert "主题" in page

        catalog = client.get("/api/catalog", params={"page_size": 200}).json()
        assert all(item["subject"] is None for item in catalog["records"])

        preview = client.get("/api/rules").json()
        assert preview["would_change_count"] >= 1
        result = client.post(
            "/api/rules/confirm",
            data={
                "rules_version": preview["rules_version"],
                "catalog_version": preview["catalog_version"],
            },
        )
        assert result.status_code == 200
        assert result.json()["confirmed"] is True

        catalog = client.get("/api/catalog", params={"q": "数据治理"}).json()
        assert catalog["records"][0]["subject"] == "数据管理"
        assert "数据管理" in client.get("/").text


def test_rule_crud_via_api_and_validation(web_app):
    with TestClient(web_app) as client:
        created = client.post(
            "/api/rules",
            data={"keyword": "图书馆", "subject": "图书馆事业", "priority": "5"},
        )
        assert created.status_code == 201
        rule_id = created.json()["rule"]["id"]

        bad = client.post(
            "/api/rules",
            data={"keyword": "  ", "subject": "x", "priority": "1"},
        )
        assert bad.status_code == 422
        not_int = client.post(
            "/api/rules",
            data={"keyword": "k", "subject": "s", "priority": "高"},
        )
        assert not_int.status_code == 422

        updated = client.post(
            "/api/rules",
            data={
                "id": str(rule_id),
                "keyword": "图书馆",
                "subject": "图书馆事业",
                "priority": "99",
            },
        )
        assert updated.status_code == 201
        assert updated.json()["rule"]["priority"] == 99

        deleted = client.delete(f"/api/rules/{rule_id}")
        assert deleted.status_code == 200
        assert client.delete(f"/api/rules/{rule_id}").status_code == 404


def test_stale_confirmation_is_rejected_with_current_preview(web_app):
    with TestClient(web_app) as client:
        client.post("/api/harvest")
        _poll(client, lambda p: p["active"] is None and p["published"]["watermark"])
        client.post(
            "/api/rules",
            data={"keyword": "图书馆", "subject": "图书馆事业", "priority": "5"},
        )
        preview = client.get("/api/rules").json()

        # The rules change after the preview was shown.
        client.post(
            "/api/rules",
            data={"keyword": "图像", "subject": "数字人文", "priority": "5"},
        )
        stale = client.post(
            "/api/rules/confirm",
            data={
                "rules_version": preview["rules_version"],
                "catalog_version": preview["catalog_version"],
            },
        )
        assert stale.status_code == 409
        body = stale.json()
        assert body["reason"] == "stale"
        # The current preview is returned so the page can render fresh results.
        assert body["current"]["rules_version"] > preview["rules_version"]

        # Nothing was published by the rejected confirmation.
        catalog = client.get("/api/catalog", params={"page_size": 200}).json()
        assert all(item["subject"] is None for item in catalog["records"])

        # Confirming with the fresh versions now works.
        fresh = client.get("/api/rules").json()
        ok = client.post(
            "/api/rules/confirm",
            data={
                "rules_version": fresh["rules_version"],
                "catalog_version": fresh["catalog_version"],
            },
        )
        assert ok.status_code == 200


def test_confirmation_refused_while_conflict_exists(web_app):
    with TestClient(web_app) as client:
        client.post("/api/harvest")
        _poll(client, lambda p: p["active"] is None and p["published"]["watermark"])
        client.post(
            "/api/rules",
            data={"keyword": "图书馆", "subject": "图书馆事业", "priority": "10"},
        )
        client.post(
            "/api/rules",
            data={"keyword": "数据", "subject": "数据科学", "priority": "10"},
        )
        preview = client.get("/api/rules").json()
        assert preview["conflict_count"] >= 1

        refused = client.post(
            "/api/rules/confirm",
            data={
                "rules_version": preview["rules_version"],
                "catalog_version": preview["catalog_version"],
            },
        )
        assert refused.status_code == 409
        assert refused.json()["reason"] == "conflict"
        catalog = client.get("/api/catalog", params={"page_size": 200}).json()
        assert all(item["subject"] is None for item in catalog["records"])


def test_harvest_running_while_rules_edited_still_applies_committed_rules(web_app, repo):
    """Interleave: a rule edit landing during a slow harvest must not corrupt it."""
    repo.set_meta("slow_seconds", "2")
    with TestClient(web_app) as client:
        started = client.post("/api/harvest")
        assert started.status_code == 202
        _poll(client, lambda p: p["active"] is not None)

        # Edit rules while the harvest is waiting on slow pages.
        client.post(
            "/api/rules",
            data={"keyword": "图书馆", "subject": "图书馆事业", "priority": "10"},
        )
        repo.set_meta("slow_seconds", "0")
        _poll(client, lambda p: p["active"] is None and p["published"]["watermark"])

        catalog = client.get("/api/catalog", params={"page_size": 200}).json()
        library_rows = [
            item for item in catalog["records"] if "图书馆" in item["title"]
        ]
        assert library_rows
        assert all(item["subject"] == "图书馆事业" for item in library_rows)


async def test_empty_window_with_rules_still_advances_watermark(harvester, store):
    store.upsert_subject_rule("图书馆", "图书馆事业", 10)
    first = await harvester.start_run()
    await wait_for_outcome(first)
    watermark = store.published_state()["watermark"]

    await asyncio.sleep(1.05)
    second = await harvester.start_run()
    assert await wait_for_outcome(second) == "succeeded"
    assert store.published_state()["watermark"] > watermark


def test_harvest_after_preview_invalidates_pending_confirmation(web_app):
    """Interleave: a harvest publishing between preview and confirm must reject it."""
    with TestClient(web_app) as client:
        client.post("/api/harvest")
        _poll(client, lambda p: p["active"] is None and p["published"]["watermark"])
        client.post(
            "/api/rules",
            data={"keyword": "图书馆", "subject": "图书馆事业", "priority": "10"},
        )
        preview = client.get("/api/rules").json()

        # Another harvest (empty window) advances the catalogue version.
        client.post("/api/harvest")
        _poll(client, lambda p: p["active"] is None)

        stale = client.post(
            "/api/rules/confirm",
            data={
                "rules_version": preview["rules_version"],
                "catalog_version": preview["catalog_version"],
            },
        )
        assert stale.status_code == 409
        assert stale.json()["reason"] == "stale"
        # Subjects were never published by the rejected confirmation.
        catalog = client.get("/api/catalog", params={"page_size": 200}).json()
        assert all(item["subject"] is None for item in catalog["records"])


def test_no_javascript_rule_form_endpoints_work(web_app):
    with TestClient(web_app) as client:
        save = client.post(
            "/rules/save",
            data={"id": "", "keyword": "OAI", "subject": "元数据", "priority": "3"},
            follow_redirects=False,
        )
        assert save.status_code == 303
        assert save.headers["location"].startswith("/rules?message=")
        rule = client.get("/api/rules").json()["rules"][0]

        delete = client.post(f"/rules/{rule['id']}/delete", follow_redirects=False)
        assert delete.status_code == 303
        assert client.get("/api/rules").json()["rules"] == []


def _poll(client: TestClient, predicate, attempts: int = 100) -> dict:
    import time

    for _ in range(attempts):
        payload = client.get("/api/status").json()
        if predicate(payload):
            return payload
        time.sleep(0.05)
    raise AssertionError(f"condition not met; last={payload}")
