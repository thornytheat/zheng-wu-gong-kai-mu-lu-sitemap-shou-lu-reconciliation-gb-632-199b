"""按站点生效的地址规则（profile）：查询、切换、校验、回滚与幂等。

规则档案只影响「算不算同一个地址」的两个开关（路径大小写、参数顺序），
每个站点各有一份当前档案，历史批次把当时的档案随 run 一起入库，
所以季度变化永远按原来的尺子复算（见 m7_watch/diff.py）。

切换是「先校验、后落库」：
1. 用候选档案把最近几批已入库的原始行重新跑一遍 M2，检查键幂等、无重复键；
2. 校验不通过就不写库，旧档案原样保留（回滚是天然结果，另有显式恢复兜底）；
3. 成功才更新 site_rules 并记录切换日志。

幂等：切换请求的 request_key 由目标站点集合 + 目标档案 + 期望版本组成。
同一个请求重试时，若已有成功记录直接返回那条，不会再次升版本。
"""
from __future__ import annotations

import hashlib
import json

from ... import config, errors, manifest
from . import normalize as normalize_mod
from . import service as url_service


class RuleValidationError(Exception):
    """候选档案没通过历史数据校验；调用方必须保持旧档案不动。"""


def _defaults() -> dict[str, str]:
    return dict(config.SITE_PROFILE_DEFAULTS)


def effective(store) -> dict[str, dict]:
    """每个站点当前生效的档案与版本：库为准，库里没有的用站点默认值。"""
    stored = store.site_profiles() if store is not None else {}
    out: dict[str, dict] = {}
    for site in config.SITES:
        row = stored.get(site.code) or {}
        profile = row.get("profile") or _defaults().get(site.code) or config.DEFAULT_PROFILE
        out[site.code] = {"profile": profile, "version": int(row.get("version") or 1),
                          "updated_at": row.get("updated_at") or ""}
    return out


def profiles_of(store, sites: list[str] | None = None) -> dict[str, str]:
    current = effective(store)
    codes = sites or list(config.SITE_CODES)
    return {code: current[code]["profile"] for code in codes if code in current}


def versions_of(store, sites: list[str] | None = None) -> dict[str, int]:
    current = effective(store)
    codes = sites or list(config.SITE_CODES)
    return {code: current[code]["version"] for code in codes if code in current}


def profile_label(profile: str) -> str:
    return (config.ADDRESS_PROFILES.get(profile) or {}).get("label", profile)


def profile_defs() -> list[dict]:
    return [{"key": key, "label": value["label"], "note": value["note"],
             "path_case_sensitive": value["path_case_sensitive"],
             "sort_query": value["sort_query"]}
            for key, value in config.ADDRESS_PROFILES.items()]


def payload(store, settings) -> dict:
    current = effective(store)
    sites = []
    for site in config.SITES:
        row = current[site.code]
        sites.append({"code": site.code, "name": site.name, "host": site.host,
                      "profile": row["profile"],
                      "profile_label": profile_label(row["profile"]),
                      "version": row["version"], "updated_at": row["updated_at"]})
    return {"rule_version": settings.rule_version, "profiles": profile_defs(),
            "defaults": dict(config.SITE_PROFILE_DEFAULTS), "sites": sites,
            "switches": store.list_switches(12) if store is not None else []}


def request_key(sites: list[str], profile: str, expected: dict[str, int] | None) -> str:
    stem = json.dumps({"sites": sorted(sites), "profile": profile,
                       "expected": dict(sorted((expected or {}).items()))},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(stem.encode("utf-8")).hexdigest()


def _merge_profiles(current: dict[str, str], targets: list[str],
                    profile: str) -> dict[str, str]:
    merged = dict(current)
    for site in targets:
        merged[site] = profile
    return merged


def validate_candidate(store, profiles: dict[str, str], targets: list[str],
                       settings) -> dict:
    """用候选档案回放最近几批原始行：键必须幂等、每个站点键集合不得重复。

    另外核对历史批次的「档案 → 键」可复现：某批入库时记录的档案若再也算不出
    当时的键，说明数据已不可复算，此时切换会让季度对比失去依据，直接拒绝。
    """
    if store.count_runs() == 0:
        return {"runs": 0, "keys": 0, "unfinished": 0}
    recent = store.list_runs(config.RULE_SWITCH_VALIDATE_RUNS)
    runs = []
    for row in recent:
        run = store.get_run(row["run_id"])
        if run is None:
            continue
        if not set(run.get("sites") or []) & set(targets):
            continue
        runs.append(run)

    # 历史批次按入库时的档案必须能原样复算：否则季度对比失去依据。
    # 回放用整批记录的档案（不只目标站点），否则其他站会被默认值重算，误判为不可复现。
    checked_keys = 0
    for run in runs:
        recorded = dict(run.get("profiles") or {})
        if recorded and run.get("keys"):
            replay = url_service.prepare(run.get("catalog") or [],
                                         run.get("sitemap") or [], recorded)
            if [slot["key"] for slot in replay["keys"]] != [slot["key"] for slot in run["keys"]]:
                raise RuleValidationError(
                    f"历史批次 {run['run_id']} 按入库档案无法复现，切换会失去季度对比依据")

    # 候选档案本身要收敛：键幂等、每个站点不产生重复键
    for run in runs:
        prepared = url_service.prepare(run.get("catalog") or [],
                                       run.get("sitemap") or [], profiles)
        seen: set[str] = set()
        for slot in prepared["keys"]:
            key = slot["key"]
            again = normalize_mod.normalize(key, profile=slot["profile"])
            if again.url != key:
                raise RuleValidationError(
                    f"键不幂等：{key} → {again.url}（{slot['profile']}）")
            if key in seen:
                raise RuleValidationError(f"候选档案产生重复键：{key}")
            seen.add(key)
            checked_keys += 1
    return {"runs": len(runs), "keys": checked_keys, "unfinished": 0}


def _dedupe_targets(sites, store) -> list[str]:
    current = effective(store)
    if not sites:
        return list(config.SITE_CODES)
    if isinstance(sites, str):
        sites = [sites]
    unknown = [code for code in sites if code not in current]
    if unknown:
        raise errors.BadRequest(f"未知站点 {', '.join(unknown)}", code="E_BAD_SITE")
    return sorted(set(sites))


def switch(store, settings, sites=None, profile: str = "",
           expected_versions: dict[str, int] | None = None,
           reason: str = "") -> dict:
    """切到新档案：校验 → 落库 → 记日志；失败保持旧档案不动。"""
    if profile not in config.ADDRESS_PROFILES:
        raise errors.BadRequest(
            f"未知规则档案 {profile or '（空）'}，可选 {', '.join(config.PROFILE_CODES)}",
            code="E_BAD_PROFILE")
    targets = _dedupe_targets(sites, store)
    current = effective(store)
    from_profiles = {code: current[code]["profile"] for code in targets}
    key = request_key(targets, profile, expected_versions)
    switch_id = "rs-" + key[:10]

    # 幂等先于版本核对：同一个请求重试（哪怕带着已过期的期望版本）都返回第一次的结果，
    # 不重复生效、也不再升版本。
    prior = store.find_switch_by_request(key)
    if prior is not None and prior["status"] == "applied":
        return {"switch": prior, "reused": True, "detail": prior.get("detail") or {}}

    if all(from_profiles[code] == profile for code in targets):
        # 已经是目标档案：重复提交不再升版本、不再写第二条日志
        synthetic = {
            "switch_id": switch_id, "request_key": key, "sites": targets,
            "from_profiles": from_profiles, "to_profile": profile,
            "status": "applied",
            "detail": {"from": from_profiles, "to": profile, "reason": reason,
                       "noop": True, "versions": versions_of(store, targets)},
            "created_at": manifest.now_utc(),
        }
        return {"switch": synthetic, "reused": True,
                "detail": {"noop": True, "rolled_back": False}}

    if expected_versions:
        mismatched = {code: current[code]["version"] for code in targets
                      if code in expected_versions
                      and int(expected_versions[code]) != current[code]["version"]}
        if mismatched:
            raise errors.Conflict(
                "规则版本已变化，请刷新后重试：" + ", ".join(
                    f"{code} 期望 {expected_versions[code]} 实际 {version}"
                    for code, version in sorted(mismatched.items())),
                code="E_RULE_VERSION")

    candidate = _merge_profiles(_current_profiles(current), targets, profile)
    created_at = manifest.now_utc()
    detail: dict = {}
    try:
        detail = validate_candidate(store, candidate, targets, settings)
        before = store.site_profiles()
        next_versions = {code: int((before.get(code) or {}).get("version")
                                   or current[code]["version"] or 1) + 1
                         for code in targets}
        try:
            for code in targets:
                store.set_site_profiles({code: profile},
                                        version=next_versions[code],
                                        updated_at=created_at)
            applied = store.site_profiles()
            if any(applied.get(code, {}).get("profile") != profile for code in targets):
                raise RuleValidationError("写入后核对失败，档案未完全生效")
        except Exception:
            # 显式回滚：恢复切换前的档案与版本
            for code in targets:
                old = before.get(code) or {}
                store.set_site_profiles(
                    {code: old.get("profile") or _defaults().get(code)
                     or config.DEFAULT_PROFILE},
                    version=int(old.get("version") or 1), updated_at=created_at)
            raise
        detail = dict(detail)
        detail.update({"from": from_profiles, "to": profile, "reason": reason,
                       "versions": next_versions})
        record = {"switch_id": switch_id, "request_key": key, "sites": targets,
                  "from_profiles": from_profiles, "to_profile": profile,
                  "status": "applied", "detail": detail, "created_at": created_at}
        store.save_switch(record)
        return {"switch": record, "reused": False,
                "detail": {"validated": detail, "rolled_back": False}}
    except Exception as exc:
        detail = {"error": type(exc).__name__, "message": str(exc),
                  "from": from_profiles, "to": profile, "reason": reason}
        record = {"switch_id": switch_id, "request_key": key, "sites": targets,
                  "from_profiles": from_profiles, "to_profile": profile,
                  "status": "failed", "detail": detail, "created_at": created_at}
        store.save_switch(record)
        if isinstance(exc, errors.AppError):
            raise
        raise errors.Conflict(
            f"切换失败，已保持旧规则：{exc}", code="E_RULE_SWITCH_FAILED") from exc


def _current_profiles(current: dict[str, dict]) -> dict[str, str]:
    return {code: row["profile"] for code, row in current.items()}
