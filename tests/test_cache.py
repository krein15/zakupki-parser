"""Notice cache: when a stored copy counts as fresh."""

from __future__ import annotations

from datetime import date

import pytest

from zkparser.cache import NoticeCache
from zkparser.website.search import SearchHit

NUMBER = "0167200003426008101"


def hit(updated: date | None = date(2026, 9, 30), number: str = NUMBER) -> SearchHit:
    return SearchHit(reg_number=number, url="", updated=updated)


def test_stored_notice_is_fresh(tmp_path):
    with NoticeCache(tmp_path) as cache:
        path = cache.store(hit(), b"<?xml version='1.0'?><a/>")
        assert path == tmp_path / "notices" / f"{NUMBER}.xml"
        assert cache.is_fresh(hit())
        assert cache.load(NUMBER) == b"<?xml version='1.0'?><a/>"


def test_unknown_notice_is_not_fresh(tmp_path):
    with NoticeCache(tmp_path) as cache:
        assert not cache.is_fresh(hit())
        assert cache.load(NUMBER) is None


def test_later_update_makes_the_copy_stale(tmp_path):
    with NoticeCache(tmp_path) as cache:
        cache.store(hit(date(2026, 9, 30)), b"<?xml?>")
        assert not cache.is_fresh(hit(date(2026, 10, 1)))
        assert cache.is_fresh(hit(date(2026, 9, 29)))


def test_deleted_file_makes_the_copy_stale(tmp_path):
    with NoticeCache(tmp_path) as cache:
        cache.store(hit(), b"<?xml?>")
        cache.path(NUMBER).unlink()
        assert not cache.is_fresh(hit())


def test_index_survives_reopening(tmp_path):
    with NoticeCache(tmp_path) as cache:
        cache.store(hit(), b"<?xml?>")
    with NoticeCache(tmp_path) as cache:
        assert cache.is_fresh(hit())


def test_hit_without_update_date_trusts_the_copy(tmp_path):
    with NoticeCache(tmp_path) as cache:
        cache.store(hit(), b"<?xml?>")
        assert cache.is_fresh(hit(updated=None))


@pytest.mark.parametrize("number", ["../../evil", "12/34", ""])
def test_rejects_numbers_that_are_not_numbers(tmp_path, number):
    with NoticeCache(tmp_path) as cache, pytest.raises(ValueError):
        cache.path(number)
