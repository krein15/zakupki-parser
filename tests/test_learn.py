"""Learning from "Не моё": the offers, and the profile file changed in place."""

from __future__ import annotations

from pathlib import Path

import pytest

from zkparser.learn import FILTER, MINUS, Suggestion, apply, merge_learned, set_list, suggest
from zkparser.profiles import Profile, ProfileError, load_profile

PROFILE = Profile(name="Разработка", regions=("72000000000",), keywords=("информационная система", "сайт"),
                  path=Path("p.toml"))
FILE = """# Мой словарь разработки: комментарии остаются.
name = "Разработка"
regions = ["72"]
keywords = [
    "информационная система",  # главное
    "сайт",
]
minus = ["поставка"]
"""


def test_a_filter_the_title_hits_comes_first_and_its_words_are_not_offered_again():
    offers = suggest(PROFILE, "Оказание услуги по предоставлению сертификатов активации сервиса технической поддержки")
    assert offers == [Suggestion(FILTER, "licences"), Suggestion(MINUS, "сервис")]
    assert offers[0].label == "фильтр «Перепродажа лицензий»"


def test_words_of_the_profile_and_of_taken_notices_are_not_offered():
    title = "Сопровождение информационной системы и сайта учреждения"
    assert [offer.value for offer in suggest(PROFILE, title)] == ["сопровождение"]
    assert suggest(PROFILE, title, ["Сопровождение сайта школы"]) == []


def test_nouns_that_say_nothing_about_the_goods_are_not_offered():
    drugs = Profile(name="Перевязка", regions=("72000000000",), keywords=("бинт*",), path=Path("b.toml"))
    title = "Поставка лекарственного препарата для медицинского применения Лактулоза"
    assert [offer.value for offer in suggest(drugs, title) if offer.kind == MINUS] == ["препарат", "лактулоза"]


def test_at_most_three_offers():
    title = "Аттестация объекта информатизации, проверка безопасности, экспертиза защищённости"
    assert len(suggest(PROFILE, title)) == 3


def test_nothing_is_offered_when_a_strong_word_keeps_the_notice():
    # "доработк* программн*" outweighs "лицензи*" in the title: no minus word or filter would keep it away.
    strong = Profile(name="Разработка", regions=("72000000000",), keywords=("информационная система",),
                     strong=('"доработк* программн*"',), path=Path("p.toml"))
    assert suggest(strong, "Доработка программного обеспечения и продление лицензии") == []


def test_a_filter_that_hits_the_niche_itself_is_not_offered():
    medical = Profile(name="Медизделия", regions=("72000000000",), keywords=("медицинск* издели*",),
                      path=Path("m.toml"))
    offers = suggest(medical, "Поставка медицинских расходных материалов для лаборатории")
    assert Suggestion(FILTER, "medicine") not in offers
    assert offers  # the nouns are still offered


def test_a_filter_that_hits_a_taken_notice_is_not_offered():
    title = "Продление лицензии на антивирус"
    assert Suggestion(FILTER, "licences") in suggest(PROFILE, title)
    offers = suggest(PROFILE, title, ["Разработка сайта и предоставление права использования"])
    assert Suggestion(FILTER, "licences") not in offers
    assert Suggestion(MINUS, "лицензия") in offers


@pytest.fixture
def profile_file(tmp_path):
    path = tmp_path / "Разработка.toml"
    path.write_text(FILE, encoding="utf-8")
    return path


def test_a_minus_word_goes_into_the_file_and_the_comments_stay(profile_file):
    assert apply(profile_file, Suggestion(MINUS, "аттестация"))
    text = profile_file.read_text(encoding="utf-8")
    assert '# Мой словарь разработки: комментарии остаются.' in text and '# главное' in text
    assert load_profile(profile_file).minus == ("поставка", "аттестация")
    assert not apply(profile_file, Suggestion(MINUS, "аттестация"))  # already there
    assert apply(profile_file, Suggestion(MINUS, "аттестация"), undo=True)
    assert load_profile(profile_file).minus == ("поставка",)


def test_a_filter_is_added_as_a_new_list(profile_file):
    assert apply(profile_file, Suggestion(FILTER, "legal_systems"))
    assert load_profile(profile_file).filters == ("legal_systems",)
    assert profile_file.read_text(encoding="utf-8").rstrip().endswith('filters = ["legal_systems"]')


def test_a_change_that_breaks_the_profile_is_undone(profile_file):
    with pytest.raises(ProfileError, match="нет фильтра"):
        apply(profile_file, Suggestion(FILTER, "нет такого"))
    assert profile_file.read_text(encoding="utf-8") == FILE


def test_set_list_replaces_a_list_written_over_several_lines():
    text = set_list(FILE, "keywords", ["сайт"])
    assert 'keywords = ["сайт"]' in text
    assert "информационная система" not in text
    assert text.endswith('minus = ["поставка"]\n')


def test_the_window_keeps_what_the_bot_changed_while_its_form_was_open(profile_file):
    loaded = load_profile(profile_file)  # the window reads the file
    apply(profile_file, Suggestion(MINUS, "аттестация"))  # a tap in Telegram
    apply(profile_file, Suggestion(FILTER, "legal_systems"))
    apply(profile_file, Suggestion(MINUS, "поставка"), undo=True)  # an earlier word taken back
    form = Profile(**{**loaded.__dict__, "keywords": ("сайт",), "minus": ("поставка", "ремонт")})  # edited in the form
    merged, kept = merge_learned(form, loaded, load_profile(profile_file))
    assert merged.keywords == ("сайт",)  # the form's own fields stay the form's
    assert merged.minus == ("ремонт", "аттестация")
    assert merged.filters == ("legal_systems",)
    assert kept == ["минус-слово «аттестация»", "убрано: минус-слово «поставка»", "фильтр «Справочные системы»"]


def test_nothing_changed_in_the_file_leaves_the_form_as_it_is(profile_file):
    loaded = load_profile(profile_file)
    form = Profile(**{**loaded.__dict__, "minus": ()})
    assert merge_learned(form, loaded, load_profile(profile_file)) == (form, [])
