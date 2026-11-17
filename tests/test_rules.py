"""M2 按站地址规则：档案开关、旧键保留、规范化试算。

改版后路径大小写与重复参数顺序区分页面；旧口径仍要能算出旧键。
"""
from __future__ import annotations

from app import config
from app.modules.m2_url import normalize, rules


def test_strict_profile_keeps_path_case_and_param_order():
    raw = "http://www.hz.gov.cn/site-a/Notice/N-1/?b=2&a=1&a=0"
    norm = normalize.normalize(raw, profile=config.PROFILE_STRICT)
    assert norm.url == "http://www.hz.gov.cn/site-a/Notice/N-1?b=2&a=1&a=0"
    # 路径大小写与参数顺序是页面标识的一部分，不再算「不规范」
    assert "case" not in norm.notes
    assert "param_order" not in norm.notes


def test_legacy_profile_still_folds_case_and_sorts_params():
    raw = "http://www.hz.gov.cn/site-a/Notice/N-1/?b=2&a=1"
    norm = normalize.normalize(raw, profile=config.PROFILE_LEGACY)
    assert norm.url == "http://www.hz.gov.cn/site-a/notice/n-1?a=1&b=2"
    assert "case" in norm.notes and "param_order" in norm.notes


def test_strict_result_carries_legacy_key():
    """新口径的键不覆盖旧键：历史批次复算与跨版本比较都靠 legacy_key。"""
    norm = normalize.normalize("http://www.hz.gov.cn/site-a/Notice/N-1?b=2&a=1",
                               profile=config.PROFILE_STRICT)
    assert norm.key == "http://www.hz.gov.cn/site-a/Notice/N-1?b=2&a=1"
    assert norm.legacy_key == "http://www.hz.gov.cn/site-a/notice/n-1?a=1&b=2"
    assert norm.legacy_key != norm.key


def test_duplicate_param_order_is_a_distinct_page_in_strict_only():
    first = normalize.normalize("http://www.hz.gov.cn/site-a/x?a=1&a=2",
                                profile=config.PROFILE_STRICT)
    second = normalize.normalize("http://www.hz.gov.cn/site-a/x?a=2&a=1",
                                 profile=config.PROFILE_STRICT)
    assert first.key != second.key
    # 旧口径仍把它们合成同址（历史数据按旧版复算的依据）
    assert first.legacy_key == second.legacy_key


def test_tracking_and_fragment_still_dropped_under_strict():
    norm = normalize.normalize(
        "http://www.hz.gov.cn/site-a/X/?utm_source=a&keep=1#top",
        profile=config.PROFILE_STRICT)
    assert norm.url == "http://www.hz.gov.cn/site-a/X?keep=1"
    assert set(norm.notes) >= {"trailing_slash", "tracking", "fragment"}


def test_rules_for_rejects_unknown_profile():
    import pytest
    with pytest.raises(ValueError):
        normalize.rules_for("nope")


def test_site_profile_defaults_are_legacy():
    assert config.SITE_PROFILE_DEFAULTS == {code: config.PROFILE_LEGACY
                                            for code in config.SITE_CODES}
    assert rules.profiles_of(None) == {code: config.PROFILE_LEGACY
                                       for code in config.SITE_CODES}


def test_normalize_endpoint_reports_profile_and_legacy_key(client):
    body = client.post("/api/url/normalize",
                       json={"url": "http://www.hz.gov.cn/site-a/Notice/N-1?b=2&a=1",
                             "profile": "strict"}).json()
    assert body["profile"] == "strict"
    assert body["key"] == "http://www.hz.gov.cn/site-a/Notice/N-1?b=2&a=1"
    assert body["legacy_key"] == "http://www.hz.gov.cn/site-a/notice/n-1?a=1&b=2"


def test_normalize_endpoint_uses_site_profile(client):
    body = client.post("/api/url/normalize",
                       json={"url": "http://www.hz.gov.cn/site-a/Notice/N-1",
                             "site": "site-a"}).json()
    assert body["profile"] == config.PROFILE_LEGACY
    assert body["key"] == "http://www.hz.gov.cn/site-a/notice/n-1"


def test_normalize_endpoint_rejects_unknown_profile_and_site(client):
    assert client.post("/api/url/normalize",
                       json={"url": "http://www.hz.gov.cn/site-a/x",
                             "profile": "nope"}).status_code == 400
    assert client.post("/api/url/normalize",
                       json={"url": "http://www.hz.gov.cn/site-a/x",
                             "site": "site-z"}).status_code == 400


def test_contract_lists_profiles():
    payload = client_contract()
    keys = {item["key"] for item in payload["profiles"]}
    assert keys == set(config.PROFILE_CODES)
    assert payload["default_profile"] == config.PROFILE_LEGACY


def client_contract():
    from app.modules.m2_url import contract
    return contract.contract_payload()
