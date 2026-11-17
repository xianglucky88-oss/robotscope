import pytest

def test_sitemap_is_open_gap():
    from src.robotscope import sitemap
    with pytest.raises(NotImplementedError):
        sitemap.process({"id": "x"})

def test_policy_engine_is_open_gap():
    from src.robotscope import policy_engine
    with pytest.raises(NotImplementedError):
        policy_engine.process({"id": "x"})
