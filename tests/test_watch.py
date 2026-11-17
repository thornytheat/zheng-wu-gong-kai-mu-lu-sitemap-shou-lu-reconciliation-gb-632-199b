"""M7 巡检变更监测：分桶表、基准选择、跳转环与基准不同两条边界。

数值来自 tests/fixtures/expected.py 与 sample_data/watch/baseline.json：
样例快照把 2 条当时死链、2 条当时可达、2 条当时 200 各改了一处，
所以「已修复」「新增死链」「新恶化」都有真实来源，不是空跑。
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from app import errors, reasons
from app.modules.m5_batches import service as batch_service
from app.modules.m7_watch import chain, diff
from app.modules.m7_watch import service as watch_service
from fixtures.expected import DEAD, PROBED

SAMPLE_MISSING = "http://www.hz.gov.cn/site-a/notice/n-2024-005"


def _row(key: str, status: int, hop_count: int = 0, site: str = "site-a",
         raws: list[dict] | None = None) -> dict:
    return {"key": key, "url": key, "site": site, "status": status,
            "hop_count": hop_count, "category": "OK", "final_url": key,
            "chain": {}, "raws": raws or []}


def _snap(rows: list[dict], run_id: str = "ra-test000001",
          started_at: str = "2026-08-16T02:00:00Z") -> dict:
    return {"schema": diff.SNAPSHOT_SCHEMA, "run_id": run_id, "started_at": started_at,
            "rule_version": "sitemapaudit/1.0.0", "sites": ["site-a"], "note": "",
            "row_count": len(rows), "rows": rows}


def test_snapshot_uses_canonical_key_and_three_states(run):
    doc = diff.snapshot(run)
    assert doc["schema"] == diff.SNAPSHOT_SCHEMA
    assert doc["row_count"] == PROBED
    keys = [row["key"] for row in doc["rows"]]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))
    assert all(key == chain.canonical_input(key) for key in keys)
    states = [diff.state_of(row) for row in doc["rows"]]
    assert states.count(diff.STATE_DEAD) == DEAD
    assert set(states) <= set(diff.STATES)
    assert sum(1 for state in states if state == diff.STATE_OK) + \
        states.count(diff.STATE_DEAD) + states.count(diff.STATE_REDIRECT) == PROBED


def test_bucket_table_covers_all_nine_combinations():
    assert len(diff.TRANSITIONS) == 9
    for prev in diff.STATES:
        for cur in diff.STATES:
            bucket = diff.bucket_of(prev, cur)
            assert bucket in reasons.CHANGE_BUCKETS
    assert diff.bucket_of(diff.STATE_DEAD, diff.STATE_REDIRECT) == "improved"
    assert diff.bucket_of(diff.STATE_REDIRECT, diff.STATE_DEAD) == "new_dead"
    assert diff.bucket_of(diff.STATE_DEAD, diff.STATE_DEAD) == "still_dead"
    with pytest.raises(ValueError):
        diff.bucket_of("nope", diff.STATE_OK)


def test_compare_splits_added_and_removed():
    prev = _snap([_row("http://h/site-a/gone", 200)])
    cur = _snap([_row("http://h/site-a/fresh", 200)])
    result = diff.compare(prev, cur)
    assert [row["bucket"] for row in result["rows"]] == ["added", "removed"]
    counts = result["summary"]["bucket_counts"]
    assert counts["added"] == 1
    assert counts["removed"] == 1
    assert result["rows"][0]["prev_state_label"] == "—"


def test_trap28_relative_base_shift_is_basis_not_add_and_remove():
    """TRAP-28：同一相对写法按目录页与 sitemap 补全出不同键，不能算成新增 + 消失。"""
    prev = _snap([_row("http://h/site-a/pages/x.html", 200, raws=[
        {"raw": "./x.html", "base_kind": chain.BASE_PAGE,
         "base": "http://h/site-a/pages/catalog.html"}])])
    cur = _snap([_row("http://h/site-a/x.html", 200, raws=[
        {"raw": "./x.html", "base_kind": chain.BASE_SITEMAP,
         "base": "http://h/site-a/sitemap.xml"}])])
    result = diff.compare(prev, cur)
    assert [row["bucket"] for row in result["rows"]] == ["basis"]
    counts = result["summary"]["bucket_counts"]
    assert counts["added"] == 0 and counts["removed"] == 0
    row = result["rows"][0]
    assert row["raw"] == "./x.html"
    assert row["prev_base"].endswith("/site-a/pages/catalog.html")
    assert row["cur_base"].endswith("/site-a/sitemap.xml")


def test_trap28_absolute_raw_is_never_basis_pair():
    """TRAP-28 反例：绝对写法按原样解析，同一个字符串不会因基准不同换键。"""
    raws = [{"raw": "http://h/site-a/x", "base_kind": chain.BASE_PAGE,
             "base": "http://h/site-a/catalog.html"}]
    prev = _snap([_row("http://h/site-a/pages/x.html", 200, raws=raws)])
    cur = _snap([_row("http://h/site-a/x.html", 200, raws=raws)])
    result = diff.compare(prev, cur)
    assert result["summary"]["bucket_counts"]["basis"] == 0
    assert result["summary"]["bucket_counts"]["added"] == 1
    assert result["summary"]["bucket_counts"]["removed"] == 1


def test_trap27_cycle_grouped_once_and_long_chain_is_not_cycle():
    """TRAP-27：同环多入口只报一次；超过上限但仍未回到已访问地址的记链过长，不是环。"""
    hops = [{"from": "http://h/a", "to": "http://h/b"},
            {"from": "http://h/b", "to": "http://h/a/"}]
    info = chain.analyze(hops)
    assert info["kind"] == chain.KIND_CYCLE
    assert info["reason"] == "CHAIN_CYCLE"
    members = info["members"]
    groups = chain.group_cycles([
        {"key": "http://h/a", "site": "site-a",
         "chain": {"kind": chain.KIND_CYCLE, "members": members}},
        {"key": "http://h/b", "site": "site-a",
         "chain": {"kind": chain.KIND_CYCLE, "members": members}},
    ])
    assert len(groups) == 1
    assert groups[0]["count"] == 2

    long_hops = [{"from": f"http://h/p{index}", "to": f"http://h/p{index + 1}"}
                 for index in range(6)]
    stretched = chain.analyze(long_hops, max_hops=5)
    assert stretched["kind"] == chain.KIND_LIMIT
    assert stretched["kind"] != chain.KIND_CYCLE
    assert stretched["reason"] == "CHAIN_LIMIT"


def test_case_and_trailing_slash_variant_keeps_one_key():
    assert chain.canonical_input("HTTP://WWW.HZ.GOV.CN/Site-A/Notice/N-1/") == \
        chain.canonical_input("http://www.hz.gov.cn/site-a/notice/n-1")
    prev = _snap([_row("http://h/site-a/x/", 200)])
    cur = _snap([_row("http://h/site-a/X", 200)])
    result = diff.compare(prev, cur)
    assert len(result["rows"]) == 1
    assert result["rows"][0]["bucket"] == "unchanged"


def test_store_previous_run_is_preferred_over_sample(settings, store):
    first = batch_service.run(settings, store, sites=["site-a"])["run"]
    second = batch_service.run(settings, store)["run"]
    assert first["run_id"] != second["run_id"]
    result = watch_service.watch(settings, store, second)
    assert result["base"]["source"] == "store"
    assert result["base"]["run_id"] == first["run_id"]
    assert result["summary"]["total"] > 0
    assert result["code"] == ""


def test_sample_baseline_used_when_store_has_only_current(settings, store, tmp_path):
    doc = _snap([_row(SAMPLE_MISSING, 404)], run_id="ra-sample0001")
    doc["note"] = "上季度样例"
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    tuned = dataclasses.replace(settings, baseline_path=str(path))
    run = batch_service.run(tuned, store)["run"]
    result = watch_service.watch(tuned, store, run)
    assert result["base"]["source"] == "sample"
    assert result["code"] == "WATCH_SAMPLE"
    assert result["base"]["label"] == "上季度样例"
    assert result["summary"]["bucket_counts"]["recovered"] >= 1


def test_missing_baseline_reports_no_base(settings, store, tmp_path):
    tuned = dataclasses.replace(settings, baseline_path=str(tmp_path / "missing.json"))
    run = batch_service.run(tuned, store)["run"]
    result = watch_service.watch(tuned, store, run)
    assert result["status"] == "no_base"
    assert result["code"] == "WATCH_NO_BASE"
    assert result["rows"] == []
    assert result["summary"]["total"] == 0
    assert result["cycles"] == []


def test_filters_and_bad_params_rejected(settings, store, run):
    result = watch_service.watch(settings, store, run, bucket="new_dead")
    assert result["rows"]
    assert all(row["bucket"] == "new_dead" for row in result["rows"])
    by_site = watch_service.watch(settings, store, run, site="site-c")
    assert all(row["site"] == "site-c" for row in by_site["rows"])
    with pytest.raises(errors.BadRequest):
        watch_service.watch(settings, store, run, bucket="nope")
    with pytest.raises(errors.BadRequest):
        watch_service.watch(settings, store, run, source="nope")
    with pytest.raises(errors.BadRequest):
        watch_service.watch(settings, store, run, site="site-z")


def test_watch_page_api_csv_and_snapshot(client_with_run):
    body = client_with_run.get("/api/watch").json()
    assert body["status"] == "ok"
    assert body["base"]["source"] in ("store", "sample")
    assert body["summary"]["total"] > 0

    page = client_with_run.get("/watch")
    assert page.status_code == 200
    assert "变更监测" in page.text
    assert "新增死链" in page.text

    csv_response = client_with_run.get("/api/watch/regressions.csv")
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv_response.headers["content-disposition"]
    lines = csv_response.text.lstrip("\ufeff").splitlines()
    assert lines[0].split(",")[:4] == ["批次", "变化", "站点", "上次"]

    snap = client_with_run.get("/api/watch/snapshot.json")
    assert snap.status_code == 200
    doc = snap.json()
    assert doc["schema"] == diff.SNAPSHOT_SCHEMA
    assert doc["row_count"] == PROBED

    assert client_with_run.get("/api/watch?bucket=nope").status_code == 400
    assert client_with_run.get("/api/watch?base=nope").status_code == 400
    assert client_with_run.get("/api/watch?site=site-z").status_code == 400


def test_watch_page_without_run_shows_empty(client):
    page = client.get("/watch")
    assert page.status_code == 200
    assert "还没有巡检批次" in page.text
    assert client.get("/api/watch").status_code == 404


def test_trend_counts_match_probe_states(run):
    doc = diff.trend([run])
    assert doc["sites"] == ["site-a", "site-b", "site-c"]
    row = doc["runs"][0]
    assert row["total"] == PROBED
    assert sum(sum(cell.values()) for cell in row["sites"].values()) == PROBED
    assert sum(cell["dead"] for cell in row["sites"].values()) == DEAD


# -- 跨版本比较：拆键 / 并键 / 待核实 ---------------------------------------

def _versioned(key: str, legacy: str, status: int, site: str = "site-a",
               profile: str = "strict", raws: list[dict] | None = None) -> dict:
    row = _row(key, status, site=site, raws=raws)
    row["legacy_key"] = legacy
    row["profile"] = profile
    return row


def _snap_v2(rows: list[dict], profiles: dict[str, str],
             run_id: str = "ra-v2") -> dict:
    doc = _snap(rows, run_id=run_id)
    doc["profiles"] = profiles
    return doc


def test_split_keys_are_not_added_or_recovered():
    """旧口径合并的一条，新口径拆成多条：不得算成新增，也不得算成修复。"""
    legacy_key = "http://h/site-a/notice/n-1"
    prev = _snap_v2([_versioned(legacy_key, legacy_key, 404, profile="legacy")],
                    {"site-a": "legacy"})
    cur = _snap_v2([
        _versioned("http://h/site-a/Notice/N-1", legacy_key, 404),
        _versioned(legacy_key, legacy_key, 200),
    ], {"site-a": "strict"})
    result = diff.compare(prev, cur)
    buckets = {row["key"]: row["bucket"] for row in result["rows"]}
    assert buckets["http://h/site-a/Notice/N-1"] == "resplit"
    assert buckets[legacy_key] == "recovered"
    counts = result["summary"]["bucket_counts"]
    assert counts["added"] == 0
    assert counts["resplit"] == 1


def test_ambiguous_split_is_unverified_not_recovered():
    """一拆多且原始写法无法唯一对应：新出现的键列待核实，不计修复。"""
    legacy_key = "http://h/site-a/notice/n-1"
    prev = _snap_v2([_versioned(legacy_key, legacy_key, 404, profile="legacy")],
                    {"site-a": "legacy"})
    cur = _snap_v2([
        _versioned("http://h/site-a/Notice/N-1", legacy_key, 404,
                   raws=[{"raw": "notice/N-1", "base_kind": "page",
                          "base": "http://h/site-a/catalog.html"}]),
        _versioned("http://h/site-a/NOTICE/n-1", legacy_key, 200,
                   raws=[{"raw": "NOTICE/n-1", "base_kind": "page",
                          "base": "http://h/site-a/catalog.html"}]),
    ], {"site-a": "strict"})
    result = diff.compare(prev, cur)
    buckets = {row["key"]: row["bucket"] for row in result["rows"]}
    # 两条新键都无法被认定是原地址的延续：都列待核实，并各自给出可能的上次键
    assert buckets == {
        "http://h/site-a/Notice/N-1": "unverified",
        "http://h/site-a/NOTICE/n-1": "unverified",
    }
    assert all(row["prev_key"] == legacy_key for row in result["rows"])
    assert result["summary"]["bucket_counts"]["recovered"] == 0
    assert result["summary"]["bucket_counts"]["unverified"] == 2


def test_unmatched_row_under_rule_change_is_unverified_not_removed():
    prev = _snap_v2([_versioned("http://h/site-a/x", "http://h/site-a/x", 404,
                                profile="legacy")], {"site-a": "legacy"})
    cur = _snap_v2([], {"site-a": "strict"})
    result = diff.compare(prev, cur)
    assert [row["bucket"] for row in result["rows"]] == ["unverified"]
    assert result["summary"]["bucket_counts"]["removed"] == 0
    assert result["summary"]["bucket_counts"]["unverified"] == 1


def test_legacy_key_match_records_basis_of_correspondence():
    """旧批用旧键、新批按新键：经旧键对应，行上给出对应依据与两侧键。"""
    prev = _snap_v2([_versioned("http://h/site-a/notice/n-1",
                                "http://h/site-a/notice/n-1", 500,
                                profile="legacy")], {"site-a": "legacy"})
    cur = _snap_v2([_versioned("http://h/site-a/Notice/N-1",
                               "http://h/site-a/notice/n-1", 200)],
                   {"site-a": "strict"})
    result = diff.compare(prev, cur)
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["bucket"] == "recovered"
    assert row["match"] == "legacy"
    assert row["match_label"] == "旧键对应"
    assert row["prev_key"] == "http://h/site-a/notice/n-1"
    assert row["key"] == "http://h/site-a/Notice/N-1"
    assert row["legacy_key"] == "http://h/site-a/notice/n-1"


def test_duplicate_param_order_is_distinct_address_under_strict():
    legacy = "http://h/site-a/x?a=1&a=2"
    prev = _snap_v2([_versioned(legacy, legacy, 200, profile="legacy")],
                    {"site-a": "legacy"})
    cur = _snap_v2([
        _versioned("http://h/site-a/x?a=1&a=2", legacy, 200,
                   raws=[{"raw": "x?a=1&a=2", "base_kind": "page",
                          "base": "http://h/site-a/catalog.html"}]),
        _versioned("http://h/site-a/x?a=2&a=1", legacy, 200,
                   raws=[{"raw": "x?a=2&a=1", "base_kind": "page",
                          "base": "http://h/site-a/catalog.html"}]),
    ], {"site-a": "strict"})
    result = diff.compare(prev, cur)
    counts = result["summary"]["bucket_counts"]
    assert counts["added"] == 0
    assert counts["resplit"] == 1
    order_row = next(row for row in result["rows"]
                     if row["key"].endswith("?a=2&a=1"))
    assert order_row["bucket"] == "resplit"
    assert order_row["legacy_key"] == legacy


def test_profile_change_flag_reports_switched_sites():
    prev = _snap_v2([], {"site-a": "legacy"})
    cur = _snap_v2([], {"site-a": "strict"})
    result = diff.compare(prev, cur)
    assert result["profile_changes"] == {"site-a": True}
    same = diff.compare(cur, cur)
    assert same["profile_changes"] == {}


def test_snapshot_keeps_real_key_and_legacy_key(settings, store):
    from app.modules.m5_batches import service as batch_service
    from app.modules.m2_url import rules
    batch_service.run(settings, store, sites=["site-a"])
    rules.switch(store, settings, sites=["site-a"], profile="strict")
    run = batch_service.run(settings, store, sites=["site-a"])["run"]
    doc = diff.snapshot(run)
    assert doc["profiles"] == {"site-a": "strict"}
    upper = next(row for row in doc["rows"]
                 if row["key"] == "http://www.hz.gov.cn/site-a/Notice/N-2024-003")
    assert upper["legacy_key"] == "http://www.hz.gov.cn/site-a/notice/n-2024-003"
    assert upper["profile"] == "strict"


def test_cross_version_watch_keeps_quarters_comparable(settings, store):
    """真实批次：切规则后复算季度变化，拆键不冒充新增/修复，导出带对应依据。"""
    from app.modules.m5_batches import service as batch_service
    from app.modules.m2_url import rules
    first = batch_service.run(settings, store, sites=["site-a"])["run"]
    rules.switch(store, settings, sites=["site-a"], profile="strict")
    second = batch_service.run(settings, store, sites=["site-a"])["run"]
    result = watch_service.watch(settings, store, second, source="store")
    counts = result["summary"]["bucket_counts"]
    assert counts["added"] == 0
    assert counts["removed"] == 0
    assert counts["resplit"] >= 1
    assert result["base"]["run_id"] == first["run_id"]
    assert result["profile_changes"] == {"site-a": True}
    row = next(row for row in result["rows"] if row["bucket"] == "resplit")
    assert row["match"] == "resplit"
    assert row["legacy_key"]
    csv_text = watch_service.regressions_csv(result)
    header = csv_text.lstrip("﻿").splitlines()[0].split(",")
    assert "对应依据" in header
    assert "旧键" in header


def test_regressions_exclude_split_and_include_unverified():
    from app import reasons
    assert "resplit" not in reasons.ACTIONABLE_BUCKETS
    assert "remerged" not in reasons.ACTIONABLE_BUCKETS
    assert "unverified" in reasons.ACTIONABLE_BUCKETS
    rows = [{"bucket": "resplit"}, {"bucket": "remerged"}, {"bucket": "unverified"},
            {"bucket": "recovered"}, {"bucket": "new_dead"}]
    picked = {row["bucket"] for row in diff.regressions(rows)}
    assert picked == {"unverified", "new_dead"}


def test_page_and_export_share_quarter_basis(settings, store):
    """页面与回归清单 CSV 用同一个 service.watch 结果，条数与分桶一致。"""
    from app.modules.m5_batches import service as batch_service
    from app.modules.m2_url import rules
    batch_service.run(settings, store, sites=["site-a"])
    rules.switch(store, settings, sites=["site-a"], profile="strict")
    second = batch_service.run(settings, store, sites=["site-a"])["run"]
    result = watch_service.watch(settings, store, second, source="store")
    csv_text = watch_service.regressions_csv(result)
    body = [line for line in csv_text.lstrip("﻿").splitlines()[1:] if line]
    assert len(body) == len(result["regressions"])
    # 每个分桶合计与逐行桶一致
    counts = result["summary"]["bucket_counts"]
    assert counts["resplit"] == sum(1 for row in result["rows"]
                                    if row["bucket"] == "resplit")
    assert counts["unverified"] == sum(1 for row in result["rows"]
                                       if row["bucket"] == "unverified")


def test_json_regressions_match_page_rows_without_filters(settings, store):
    from app.modules.m5_batches import service as batch_service
    from app.modules.m2_url import rules
    batch_service.run(settings, store, sites=["site-a"])
    rules.switch(store, settings, sites=["site-a"], profile="strict")
    second = batch_service.run(settings, store, sites=["site-a"])["run"]
    full = watch_service.watch(settings, store, second, source="store",
                               bucket="unverified")
    filtered = watch_service.watch(settings, store, second, source="store")
    assert full["shown"] == len(full["rows"])
    assert all(row["bucket"] == "unverified" for row in full["rows"])
    assert full["summary"]["total"] == filtered["summary"]["total"]
