"""Unit tests for the subject rule matching engine."""

from __future__ import annotations

import pytest

from app.subjects import (
    Rule,
    classify_catalog,
    classify_title,
    normalize_rule_input,
)


def r(rule_id: int, keyword: str, subject: str, priority: int) -> Rule:
    return Rule(id=rule_id, keyword=keyword, subject=subject, priority=priority)


def test_no_rules_or_no_match_leaves_subject_empty():
    assert classify_title("任意标题", []).subject is None
    verdict = classify_title("高校图书馆数据治理实践", [r(1, "医学", "医学", 10)])
    assert verdict.subject is None
    assert verdict.matched_rules == ()
    assert not verdict.conflict


def test_keyword_match_is_case_insensitive_substring():
    rules = [r(1, "OAI", "元数据互操作", 5)]
    verdict = classify_title("基于 oai-pmh 的互操作研究", rules)
    assert verdict.subject == "元数据互操作"
    assert verdict.matched_rules == (1,)


def test_highest_priority_wins_when_several_rules_match():
    rules = [
        r(1, "图书馆", "图书馆事业", 1),
        r(2, "数据治理", "数据管理", 10),
    ]
    verdict = classify_title("高校图书馆数据治理实践", rules)
    assert verdict.subject == "数据管理"
    assert verdict.matched_rules == (2,)


def test_same_subject_at_top_priority_is_not_a_conflict():
    rules = [
        r(1, "图书馆", "图书馆事业", 10),
        r(2, "参考咨询", "图书馆事业", 10),
        r(3, "其他", "其他主题", 5),
    ]
    verdict = classify_title("图书馆参考咨询服务", rules)
    assert not verdict.conflict
    assert verdict.subject == "图书馆事业"
    assert set(verdict.matched_rules) == {1, 2}


def test_different_subjects_at_top_priority_is_a_conflict():
    rules = [
        r(1, "图书馆", "图书馆事业", 10),
        r(2, "数据", "数据科学", 10),
        r(3, "低优先", "低优先主题", 1),
    ]
    verdict = classify_title("图书馆数据治理", rules)
    assert verdict.conflict
    assert verdict.subject is None
    assert set(verdict.conflict_subjects) == {"图书馆事业", "数据科学"}
    # The lower-priority rule never participates in the conflict.
    assert 3 not in verdict.matched_rules


def test_lower_priority_disagreement_is_resolved_by_priority():
    rules = [
        r(1, "图书馆", "图书馆事业", 20),
        r(2, "数据", "数据科学", 10),
    ]
    verdict = classify_title("图书馆数据治理", rules)
    assert not verdict.conflict
    assert verdict.subject == "图书馆事业"


def test_empty_title_never_matches():
    verdict = classify_title(None, [r(1, "x", "X", 1)])
    assert verdict.subject is None
    assert not verdict.conflict


def test_classify_catalog_enriches_rows():
    rows = [
        {"identifier": "a", "title": "图书馆数据治理"},
        {"identifier": "b", "title": "古籍图像检索"},
    ]
    rules = [r(1, "数据", "数据科学", 10), r(2, "图书馆", "图书馆事业", 10)]
    result = classify_catalog(rows, rules)
    by_id = {row["identifier"]: row for row in result}
    assert by_id["a"]["conflict"] is True
    assert by_id["a"]["subject"] is None
    assert by_id["b"]["subject"] is None
    assert by_id["b"]["conflict"] is False


def test_normalize_rule_input_validation():
    assert normalize_rule_input(" 关键词 ", " 主题 ", "7") == ("关键词", "主题", 7)
    with pytest.raises(ValueError):
        normalize_rule_input("  ", "主题", 1)
    with pytest.raises(ValueError):
        normalize_rule_input("关键词", " ", 1)
    with pytest.raises(ValueError):
        normalize_rule_input("关键词", "主题", "不是整数")
