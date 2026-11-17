"""Store-level behaviour of subject rules, preview and confirmation."""

from __future__ import annotations

import pytest

from app.store import Store, SubjectConflict


def _seed_catalog(store: Store, identifiers: list[tuple[str, str]]) -> None:
    run = store.create_run()
    store.set_window(run["id"], "2026-01-01T00:00:00Z")
    store.stage_records(
        run["id"],
        [
            {
                "identifier": identifier,
                "title": title,
                "creator": None,
                "datestamp": "2025-12-31T00:00:00Z",
                "deleted": False,
            }
            for identifier, title in identifiers
        ],
        0,
    )
    assert store.commit_run(run["id"])["committed"]


def _subjects(store: Store) -> dict[str, str | None]:
    return {
        row["identifier"]: row["subject"]
        for row in store.catalog_page(page_size=200)["records"]
    }


def test_rule_crud_persists_and_orders_by_priority(tmp_path):
    store = Store(tmp_path / "rules.sqlite3")
    first = store.upsert_subject_rule("图书馆", "图书馆事业", 5)
    second = store.upsert_subject_rule("数据", "数据科学", 100)
    rules = store.list_subject_rules()
    assert [row["id"] for row in rules] == [second["id"], first["id"]]

    updated = store.upsert_subject_rule("图书馆", "图书馆事业", 50, first["id"])
    assert updated["priority"] == 50
    assert [row["id"] for row in store.list_subject_rules()] == [
        second["id"],
        first["id"],
    ]
    assert store.delete_subject_rule(second["id"]) is True
    assert store.delete_subject_rule(second["id"]) is False
    assert [row["id"] for row in store.list_subject_rules()] == [first["id"]]
    store.close()


def test_every_rule_edit_bumps_rules_version(tmp_path):
    store = Store(tmp_path / "v.sqlite3")
    v0 = store.subject_rules_version()
    rule = store.upsert_subject_rule("a", "A", 1)
    assert store.subject_rules_version() == v0 + 1
    store.upsert_subject_rule("a", "A2", 2, rule["id"])
    assert store.subject_rules_version() == v0 + 2
    store.delete_subject_rule(rule["id"])
    assert store.subject_rules_version() == v0 + 3
    store.close()


def test_invalid_rule_input_rejected(tmp_path):
    store = Store(tmp_path / "bad.sqlite3")
    with pytest.raises(ValueError):
        store.upsert_subject_rule(" ", "主题", 1)
    with pytest.raises(ValueError):
        store.upsert_subject_rule("关键词", " ", 1)
    with pytest.raises(ValueError):
        store.upsert_subject_rule("关键词", "主题", "x")
    assert store.subject_rules_version() == 0
    store.close()


def test_update_unknown_rule_raises_keyerror_without_version_change(tmp_path):
    store = Store(tmp_path / "missing.sqlite3")
    v0 = store.subject_rules_version()
    with pytest.raises(KeyError):
        store.upsert_subject_rule("a", "A", 1, rule_id=9999)
    assert store.subject_rules_version() == v0
    store.close()


def test_preview_reports_changes_and_conflicts_without_writing(tmp_path):
    store = Store(tmp_path / "preview.sqlite3")
    _seed_catalog(
        store,
        [
            ("oai:1", "高校图书馆数据治理实践"),
            ("oai:2", "古籍数字化中的图像检索"),
        ],
    )
    # Two equal-priority rules disagree on oai:1.
    store.upsert_subject_rule("图书馆", "图书馆事业", 10)
    store.upsert_subject_rule("数据", "数据科学", 10)
    store.upsert_subject_rule("图像", "数字人文", 5)

    preview = store.preview_subject_rules()
    assert preview["catalog_size"] == 2
    assert preview["conflict_count"] == 1
    assert preview["conflicts"][0]["identifier"] == "oai:1"
    assert set(preview["conflicts"][0]["conflict_subjects"]) == {
        "图书馆事业",
        "数据科学",
    }
    # oai:2 would gain a subject but nothing has been published yet.
    change_ids = {row["identifier"] for row in preview["changes"]}
    assert change_ids == {"oai:2"}
    assert _subjects(store) == {"oai:1": None, "oai:2": None}
    store.close()


def test_confirm_publishes_subjects_and_bumps_catalog_version(tmp_path):
    store = Store(tmp_path / "confirm.sqlite3")
    _seed_catalog(store, [("oai:1", "高校图书馆数据治理实践"), ("oai:2", "无关键词标题")])
    store.upsert_subject_rule("数据治理", "数据管理", 10)
    preview = store.preview_subject_rules()

    result = store.confirm_subject_rules(
        rules_version=preview["rules_version"],
        catalog_version=preview["catalog_version"],
    )
    assert result["confirmed"] is True
    assert _subjects(store) == {"oai:1": "数据管理", "oai:2": None}
    assert store.catalog_version() == preview["catalog_version"] + 1
    # Confirming again with the same versions is a stale request.
    again = store.confirm_subject_rules(
        rules_version=preview["rules_version"],
        catalog_version=preview["catalog_version"],
    )
    assert again["confirmed"] is False and again["reason"] == "stale"
    store.close()


def test_confirm_rejected_when_rules_changed_after_preview(tmp_path):
    store = Store(tmp_path / "stale-rules.sqlite3")
    _seed_catalog(store, [("oai:1", "图书馆数据治理")])
    store.upsert_subject_rule("图书馆", "图书馆事业", 10)
    preview = store.preview_subject_rules()
    # The rules move while the user is reviewing the preview.
    store.upsert_subject_rule("数据", "数据科学", 10)

    result = store.confirm_subject_rules(
        rules_version=preview["rules_version"],
        catalog_version=preview["catalog_version"],
    )
    assert result["confirmed"] is False and result["reason"] == "stale"
    assert all(value is None for value in _subjects(store).values())
    store.close()


def test_confirm_rejected_when_catalog_changed_after_preview(tmp_path):
    store = Store(tmp_path / "stale-catalog.sqlite3")
    _seed_catalog(store, [("oai:1", "图书馆数据治理")])
    store.upsert_subject_rule("图书馆", "图书馆事业", 10)
    preview = store.preview_subject_rules()
    # A harvest publishes while the user is reviewing the preview.
    run = store.create_run()
    store.set_window(run["id"], "2026-01-02T00:00:00Z")
    store.stage_records(
        run["id"],
        [
            {
                "identifier": "oai:2",
                "title": "新增的图书馆论文",
                "creator": None,
                "datestamp": "2026-01-01T12:00:00Z",
                "deleted": False,
            }
        ],
        0,
    )
    assert store.commit_run(run["id"])["committed"]

    result = store.confirm_subject_rules(
        rules_version=preview["rules_version"],
        catalog_version=preview["catalog_version"],
    )
    assert result["confirmed"] is False and result["reason"] == "stale"
    store.close()


def test_confirm_with_conflict_changes_nothing(tmp_path):
    store = Store(tmp_path / "confirm-conflict.sqlite3")
    _seed_catalog(store, [("oai:1", "图书馆数据治理"), ("oai:2", "图像检索研究")])
    store.upsert_subject_rule("图书馆", "图书馆事业", 10)
    store.upsert_subject_rule("数据", "数据科学", 10)
    store.upsert_subject_rule("图像", "数字人文", 5)
    preview = store.preview_subject_rules()
    catalog_version_before = store.catalog_version()

    result = store.confirm_subject_rules(
        rules_version=preview["rules_version"],
        catalog_version=preview["catalog_version"],
    )
    assert result["confirmed"] is False and result["reason"] == "conflict"
    assert store.catalog_version() == catalog_version_before
    assert all(value is None for value in _subjects(store).values())
    store.close()


def test_harvest_commit_applies_current_rules_and_removes_subject_on_withdrawal(tmp_path):
    store = Store(tmp_path / "harvest-subject.sqlite3")
    store.upsert_subject_rule("数据治理", "数据管理", 10)
    _seed_catalog(store, [("oai:1", "高校图书馆数据治理实践")])
    assert _subjects(store)["oai:1"] == "数据管理"

    # A correction that makes the title miss the rule must clear the subject.
    run = store.create_run()
    store.set_window(run["id"], "2026-01-02T00:00:00Z")
    store.stage_records(
        run["id"],
        [
            {
                "identifier": "oai:1",
                "title": "完全改写后的新标题",
                "creator": None,
                "datestamp": "2026-01-01T12:00:00Z",
                "deleted": False,
            }
        ],
        0,
    )
    assert store.commit_run(run["id"])["committed"]
    assert _subjects(store)["oai:1"] is None

    # Withdrawal deletes the row (and therefore its subject).
    run2 = store.create_run()
    store.set_window(run2["id"], "2026-01-03T00:00:00Z")
    store.stage_records(
        run2["id"],
        [
            {
                "identifier": "oai:1",
                "title": None,
                "creator": None,
                "datestamp": "2026-01-02T12:00:00Z",
                "deleted": True,
            }
        ],
        0,
    )
    assert store.commit_run(run2["id"])["committed"]
    assert store.catalog_page()["records"] == []
    store.close()


def test_harvest_commit_conflict_rolls_back_everything(tmp_path):
    store = Store(tmp_path / "harvest-conflict.sqlite3")
    _seed_catalog(store, [("oai:1", "普通图书馆论文")])
    watermark_before = store.published_state()["watermark"]
    catalog_before = store.catalog_page(page_size=200)["records"]

    # Rules currently disagree at the top priority.
    store.upsert_subject_rule("图书馆", "图书馆事业", 10)
    store.upsert_subject_rule("数据", "数据科学", 10)

    run = store.create_run()
    store.set_window(run["id"], "2026-01-02T00:00:00Z")
    store.stage_records(
        run["id"],
        [
            {
                "identifier": "oai:2",
                "title": "图书馆数据治理新论文",
                "creator": None,
                "datestamp": "2026-01-01T12:00:00Z",
                "deleted": False,
            }
        ],
        0,
    )
    with pytest.raises(SubjectConflict):
        store.commit_run(run["id"])

    # The whole round failed: catalogue, subjects and watermark unchanged.
    assert store.published_state()["watermark"] == watermark_before
    assert store.catalog_page(page_size=200)["records"] == catalog_before
    assert store.get_run(run["id"])["status"] == "running"  # open for failure finalisation
    store.finish_failed(run["id"], "test cleanup")
    store.close()


def test_subject_column_migrated_from_legacy_database(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    import sqlite3

    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE catalog (
            identifier TEXT PRIMARY KEY, title TEXT NOT NULL, creator TEXT,
            source_datestamp TEXT NOT NULL, harvested_at TEXT NOT NULL
        );
        INSERT INTO catalog VALUES ('oai:old', '旧库记录', NULL,
            '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    store = Store(db)
    item = next(
        row
        for row in store.catalog_page(page_size=200)["records"]
        if row["identifier"] == "oai:old"
    )
    assert item["subject"] is None
    store.close()
