"""规则切换：按站生效、校验失败保持旧规则、幂等重试、显式回滚。

切换是「先校验、后落库」：候选档案回放历史原始行，键不幂等或历史批次不可复现
就拒绝，库里的旧档案原样不动；同一个请求重试命中同一 switch_id，不再升版本。
"""
from __future__ import annotations

import pytest

from app import errors
from app.modules.m2_url import rules
from app.modules.m5_batches import service as batch_service


def test_switch_only_affects_target_site(settings, store):
    batch_service.run(settings, store, sites=["site-a"])
    result = rules.switch(store, settings, sites=["site-a"], profile="strict")
    assert result["switch"]["status"] == "applied"
    current = rules.profiles_of(store)
    assert current == {"site-a": "strict", "site-b": "legacy", "site-c": "legacy"}
    assert store.site_profiles()["site-a"]["version"] == 2
    assert "site-b" not in store.site_profiles()


def test_retry_same_request_does_not_reapply(settings, store):
    batch_service.run(settings, store, sites=["site-a"])
    first = rules.switch(store, settings, sites=["site-a"], profile="strict")
    second = rules.switch(store, settings, sites=["site-a"], profile="strict")
    assert second["reused"] is True
    assert second["switch"]["switch_id"] == first["switch"]["switch_id"]
    assert store.site_profiles()["site-a"]["version"] == 2
    assert len(store.list_switches()) == 1


def test_stale_expected_version_is_conflict(settings, store):
    batch_service.run(settings, store, sites=["site-a"])
    rules.switch(store, settings, sites=["site-a"], profile="strict")
    with pytest.raises(errors.Conflict) as caught:
        rules.switch(store, settings, sites=["site-a"], profile="legacy",
                     expected_versions={"site-a": 1})
    assert caught.value.code == "E_RULE_VERSION"
    assert rules.profiles_of(store)["site-a"] == "strict"


def test_validation_failure_keeps_old_profile(settings, store, monkeypatch):
    batch_service.run(settings, store)
    original = rules.validate_candidate

    def boom(store_arg, profiles, targets, settings_arg):
        raise rules.RuleValidationError("模拟校验失败")

    monkeypatch.setattr(rules, "validate_candidate", boom)
    with pytest.raises(errors.Conflict) as caught:
        rules.switch(store, settings, sites=["site-a"], profile="strict")
    assert caught.value.code == "E_RULE_SWITCH_FAILED"
    monkeypatch.setattr(rules, "validate_candidate", original)
    assert rules.profiles_of(store)["site-a"] == "legacy"
    assert store.site_profiles() == {}
    failed = store.list_switches()[0]
    assert failed["status"] == "failed"
    assert "模拟校验失败" in failed["detail"]["message"]


def test_write_failure_rolls_back_to_previous_profile(settings, store, monkeypatch):
    batch_service.run(settings, store, sites=["site-a"])
    rules.switch(store, settings, sites=["site-a"], profile="strict")
    before = store.site_profiles()["site-a"]

    def explode(profiles, version, updated_at):
        raise RuntimeError("磁盘写入失败")

    monkeypatch.setattr(store, "set_site_profiles", explode)
    with pytest.raises(errors.Conflict) as caught:
        rules.switch(store, settings, sites=["site-a"], profile="legacy")
    assert caught.value.code == "E_RULE_SWITCH_FAILED"
    monkeypatch.undo()
    assert store.site_profiles()["site-a"] == before


def test_rollback_restores_legacy(settings, store):
    batch_service.run(settings, store, sites=["site-a"])
    rules.switch(store, settings, sites=["site-a"], profile="strict")
    result = rules.switch(store, settings, sites=["site-a"], profile="legacy",
                          reason="回滚")
    assert result["switch"]["to_profile"] == "legacy"
    assert rules.profiles_of(store)["site-a"] == "legacy"


def test_same_profile_switch_is_noop(settings, store):
    result = rules.switch(store, settings, sites=["site-a"], profile="legacy")
    assert result["reused"] is True
    assert store.count_runs() == 0
    assert store.site_profiles() == {}


def test_validation_rejects_history_that_cannot_be_reproduced(settings, store):
    run = batch_service.run(settings, store, sites=["site-a"])["run"]
    run["keys"][0]["key"] = "http://www.hz.gov.cn/site-a/tampered"
    store.save_run(run)
    with pytest.raises(errors.Conflict) as caught:
        rules.switch(store, settings, sites=["site-a"], profile="strict")
    assert caught.value.code == "E_RULE_SWITCH_FAILED"
    assert rules.profiles_of(store)["site-a"] == "legacy"


def test_unknown_profile_and_site_rejected(settings, store):
    with pytest.raises(errors.BadRequest) as caught:
        rules.switch(store, settings, sites=["site-a"], profile="nope")
    assert caught.value.code == "E_BAD_PROFILE"
    with pytest.raises(errors.BadRequest) as caught:
        rules.switch(store, settings, sites=["site-z"], profile="strict")
    assert caught.value.code == "E_BAD_SITE"


def test_no_runs_switch_is_allowed(settings, store):
    result = rules.switch(store, settings, sites=["site-a"], profile="strict")
    assert result["switch"]["status"] == "applied"
    assert result["detail"]["validated"]["runs"] == 0


def test_api_switch_and_retry(client):
    response = client.post("/api/rules/switch",
                           json={"sites": ["site-a"], "profile": "strict"})
    assert response.status_code == 200
    assert response.json()["status"] == "applied"
    retry = client.post("/api/rules/switch",
                        json={"sites": ["site-a"], "profile": "strict"})
    assert retry.json()["reused"] is True
    listing = client.get("/api/rules").json()
    site_a = next(row for row in listing["sites"] if row["code"] == "site-a")
    assert site_a["profile"] == "strict"
    assert site_a["version"] == 2


def test_api_switch_errors(client):
    bad_profile = client.post("/api/rules/switch",
                              json={"sites": ["site-a"], "profile": "nope"})
    assert bad_profile.status_code == 400
    assert bad_profile.json()["error"] == "E_BAD_PROFILE"
    missing = client.post("/api/rules/rollback", json={})
    assert missing.status_code == 400
    assert missing.json()["error"] == "E_BAD_SITE"


def test_api_rollback(client):
    client.post("/api/rules/switch", json={"sites": ["site-a"], "profile": "strict"})
    rollback = client.post("/api/rules/rollback",
                           json={"sites": ["site-a"]})
    assert rollback.status_code == 200
    assert rollback.json()["status"] == "applied"
    listing = client.get("/api/rules").json()
    assert next(row for row in listing["sites"]
                if row["code"] == "site-a")["profile"] == "legacy"


def test_rules_page_renders(client):
    page = client.get("/rules")
    assert page.status_code == 200
    assert "地址规则" in page.text
    assert "旧规则" in page.text


def test_batch_profile_enters_batch_key(client):
    """同一份字节、同一站点，换了档案就是另一批；不命中旧结果。"""
    legacy = client.post("/api/crawl", json={}).json()
    client.post("/api/rules/switch", json={"sites": ["site-a"], "profile": "strict"})
    strict = client.post("/api/crawl", json={}).json()
    assert strict["run_id"] != legacy["run_id"]
    assert strict["reused"] is False
    detail = client.get("/api/export/manifest.json").json()
    assert detail["profiles"]["site-a"] == "strict"
    assert detail["profile_version"] >= 2


def test_resume_keeps_batch_profiles(settings, store):
    """续跑沿用入库时的档案：规则后来切了也不能换老批次的尺子。"""
    partial = batch_service.run(settings, store, sites=["site-a"],
                                stop_after="crawl")["run"]
    assert partial["profiles"] == {"site-a": "legacy"}
    rules.switch(store, settings, sites=["site-a"], profile="strict")
    resumed = batch_service.run(settings, store, mode="resume",
                                resume_of=partial["run_id"])["run"]
    assert resumed["profiles"] == {"site-a": "legacy"}
    assert resumed["status"] == "done"


def test_explicit_profile_only_affects_that_batch(settings, store):
    result = batch_service.run(settings, store, sites=["site-a"],
                               profile="strict")["run"]
    assert result["profiles"] == {"site-a": "strict"}
    # 没落库：站点当前档案不动
    assert rules.profiles_of(store)["site-a"] == "legacy"


def test_retry_with_stale_expected_version_still_returns_prior(settings, store):
    """同一请求重试即使带着已过期版本，也返回第一次结果，不重复生效。"""
    batch_service.run(settings, store, sites=["site-a"])
    first = rules.switch(store, settings, sites=["site-a"], profile="strict",
                         expected_versions={"site-a": 1})
    again = rules.switch(store, settings, sites=["site-a"], profile="strict",
                         expected_versions={"site-a": 1})
    assert again["reused"] is True
    assert again["switch"]["switch_id"] == first["switch"]["switch_id"]
    assert store.site_profiles()["site-a"]["version"] == 2
    assert len(store.list_switches()) == 1


def test_different_request_after_switch_still_validates(settings, store):
    batch_service.run(settings, store, sites=["site-a"])
    rules.switch(store, settings, sites=["site-a"], profile="strict")
    with pytest.raises(errors.Conflict) as caught:
        rules.switch(store, settings, sites=["site-a"], profile="legacy",
                     expected_versions={"site-a": 1})
    assert caught.value.code == "E_RULE_VERSION"


def test_voice_switch_with_mixed_profiles_replays_whole_batch(settings, store):
    """目标站之外的档案也要按入库值回放，不能拿默认值重算后误判不可复现。"""
    batch_service.run(settings, store, sites=["site-a", "site-b"])
    rules.switch(store, settings, sites=["site-a", "site-b"], profile="strict")
    batch_service.run(settings, store, sites=["site-a", "site-b"])
    result = rules.switch(store, settings, sites=["site-b"], profile="legacy")
    assert result["switch"]["status"] == "applied"
    assert rules.profiles_of(store) == {"site-a": "strict", "site-b": "legacy",
                                        "site-c": "legacy"}
