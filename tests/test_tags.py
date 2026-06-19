"""Tests for technique-tag join/split/validation."""
from __future__ import annotations

from src.core.tags import (
    TAG_SEPARATOR,
    TECHNIQUE_TAGS,
    join_tags,
    normalize_tags,
    parse_tags,
    split_tags,
)


def test_join_uses_separator_and_keeps_valid():
    s = join_tags(["フック", "問いかけ"])
    assert s == "フック" + TAG_SEPARATOR + "問いかけ"


def test_join_drops_invalid_and_dedupes():
    s = join_tags(["フック", "存在しないタグ", "フック", "CTA"])
    assert s == TAG_SEPARATOR.join(["フック", "CTA"])  # invalid dropped, dup removed


def test_split_handles_spacing():
    assert split_tags("フック | 問いかけ|共感") == ["フック", "問いかけ", "共感"]
    assert split_tags("") == []
    assert split_tags(None) == []


def test_parse_returns_only_valid_tags():
    # stored cell may contain a stale/renamed tag -> dropped on read
    assert parse_tags("フック | 旧タグ | CTA") == ["フック", "CTA"]


def test_normalize_preserves_input_order():
    assert normalize_tags(["CTA", "フック"]) == ["CTA", "フック"]


def test_all_defined_tags_roundtrip():
    assert parse_tags(join_tags(TECHNIQUE_TAGS)) == TECHNIQUE_TAGS
