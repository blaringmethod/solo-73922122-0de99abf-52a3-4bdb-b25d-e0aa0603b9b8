"""Subject assignment rules.

A rule maps a *title keyword* to a *subject name* and carries an integer
*priority*.  A title is classified against the rules currently in force:

* every rule whose keyword occurs in the title (case-insensitive substring)
  is a candidate;
* the candidate(s) with the highest priority win;
* a single winning subject is returned when all top candidates name the same
  subject (no rule matching means the title gets no subject, ``None``);
* when the highest priority is shared by rules naming *different* subjects the
  title is in conflict.  Conflicts are never silently resolved: callers must
  surface them and refuse to publish.

The module is deliberately free of SQLite/FastAPI imports so the matching
semantics can be unit-tested in isolation and reused by both the preview
endpoint and the harvest commit transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class Rule:
    id: int
    keyword: str
    subject: str
    priority: int


@dataclass(frozen=True)
class Classification:
    subject: str | None
    matched_rules: tuple[int, ...]
    conflict_subjects: tuple[str, ...]

    @property
    def conflict(self) -> bool:
        return bool(self.conflict_subjects)


def normalize_rule_input(keyword: str, subject: str, priority: Any) -> tuple[str, str, int]:
    """Validate one rule edit, returning the canonical values."""
    keyword = (keyword or "").strip()
    subject = (subject or "").strip()
    if not keyword:
        raise ValueError("标题关键词不能为空")
    if not subject:
        raise ValueError("主题名不能为空")
    if isinstance(priority, bool) or not isinstance(priority, int):
        try:
            priority = int(str(priority).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("优先级必须是整数") from exc
    return keyword, subject, priority


def classify_title(title: str | None, rules: Sequence[Rule]) -> Classification:
    """Apply :data:`rules` to one title.

    The result is independent of rule ordering: only priorities and subjects
    decide the outcome.  A keyword may legitimately appear in several rules;
    identical ``(priority, subject)`` pairs do not conflict.
    """
    text = (title or "").casefold()
    candidates = [rule for rule in rules if rule.keyword.casefold() in text]
    if not candidates:
        return Classification(None, (), ())
    top_priority = max(rule.priority for rule in candidates)
    winners = [rule for rule in candidates if rule.priority == top_priority]
    subjects = tuple(dict.fromkeys(rule.subject for rule in winners))
    matched = tuple(dict.fromkeys(rule.id for rule in winners))
    if len(subjects) > 1:
        return Classification(None, matched, subjects)
    return Classification(subjects[0], matched, ())


def classify_catalog(
    rows: Sequence[dict[str, Any]], rules: Sequence[Rule]
) -> list[dict[str, Any]]:
    """Classify catalogue rows, attaching ``subject`` / conflict details.

    Each input row needs at least ``identifier`` and ``title``.  The output
    keeps the input keys and adds:

    * ``subject`` – the assigned subject, or ``None`` (no match / conflict);
    * ``matched_rule_ids`` – ids of the rules that decided the outcome;
    * ``conflict`` – ``True`` when top-priority rules disagree;
    * ``conflict_subjects`` – the disagreeing subject names.
    """
    results: list[dict[str, Any]] = []
    for row in rows:
        verdict = classify_title(row.get("title"), rules)
        enriched = dict(row)
        enriched["subject"] = verdict.subject
        enriched["matched_rule_ids"] = list(verdict.matched_rules)
        enriched["conflict"] = verdict.conflict
        enriched["conflict_subjects"] = list(verdict.conflict_subjects)
        results.append(enriched)
    return results
