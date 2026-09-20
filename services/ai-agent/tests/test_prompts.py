"""Part 4.2 — the prompt loader. Read once at first use, never at import."""

import pytest

import prompts


@pytest.fixture(autouse=True)
def clean():
    prompts.clear_cache()
    yield
    prompts.clear_cache()


def test_headline_classify_prompt_ships_and_states_the_contract():
    text = prompts.load(prompts.HEADLINE_CLASSIFY)
    assert text.strip()
    # Every enum the schema accepts has to be described, or the model is
    # being asked to guess what "insider" means.
    for word in ("relevance", "sentiment", "category", "oneLine"):
        assert word in text
    for value in ("high", "medium", "low"):
        assert f"`{value}`" in text
    for value in ("guidance", "analyst", "legal", "product", "macro", "insider", "other"):
        assert f"`{value}`" in text
    assert "-1" in text and "1" in text


def test_prompt_is_not_advice():
    """The model classifies; it never advises. Part 4.2 is not the analyst."""
    # normalized: the prompt is hard-wrapped prose, so a phrase can straddle
    # a line break.
    text = " ".join(prompts.load(prompts.HEADLINE_CLASSIFY).lower().split())
    assert "never give trading advice" in text
    assert "never predict a price" in text
    assert "no advice" in text


def test_load_caches_and_reload_rereads(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "PROMPT_DIR", tmp_path)
    path = tmp_path / "demo.md"
    path.write_text("first", encoding="utf-8")

    assert prompts.load("demo") == "first"
    path.write_text("second", encoding="utf-8")
    assert prompts.load("demo") == "first", "held after the first read"
    assert prompts.load("demo", reload=True) == "second"


def test_missing_or_empty_prompt_raises_prompt_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "PROMPT_DIR", tmp_path)
    with pytest.raises(prompts.PromptMissing):
        prompts.load("not_there")

    (tmp_path / "blank.md").write_text("   \n", encoding="utf-8")
    with pytest.raises(prompts.PromptMissing):
        prompts.load("blank")


def test_bad_name_is_a_value_error():
    for bad in ("", "   ", None, 3):
        with pytest.raises(ValueError):
            prompts.load(bad)


def test_clear_cache_is_selective(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "PROMPT_DIR", tmp_path)
    (tmp_path / "a.md").write_text("A", encoding="utf-8")
    (tmp_path / "b.md").write_text("B", encoding="utf-8")
    prompts.load("a")
    prompts.load("b")
    prompts.clear_cache("a")
    assert "a" not in prompts._CACHE and "b" in prompts._CACHE
