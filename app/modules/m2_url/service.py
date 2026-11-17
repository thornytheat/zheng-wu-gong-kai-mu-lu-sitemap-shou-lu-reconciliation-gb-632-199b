"""M2 编排：把目录行与收录行归一成同一把 key，并登记改写痕迹。

同一条 key 可能来自多行原始条目（大小写、跟踪参数、相对路径不同），
这里把「原始写法」全部保留成 raw_forms，供缺陷单做证据；
越界与非法地址不进 key，进隔离区。

地址规则按站点生效：调用方传入 {站点: 档案}，没传就按各站点当前生效的档案
（库里的 site_rules）。每条记录同时保留 ``legacy_key``——按旧口径算出的旧键，
历史批次复算与跨版本比较靠它对应。
"""
from __future__ import annotations

from ... import config, reasons
from . import contract, normalize, scope


def _note(notes: list[dict], site: str, code: str, detail: str) -> None:
    notes.append({"site": site, "code": code, "label": reasons.label(code),
                  "reason": reasons.label(code), "detail": detail,
                  "level": reasons.level(code)})


def _quarantine(rows: list[dict], site: str, source: str, row_no: int,
                reason: str, excerpt: str) -> None:
    rows.append({"site": site, "source": source, "row_no": row_no, "reason": reason,
                 "excerpt": (excerpt or "")[:100], "level": reasons.level(reason),
                 "label": reasons.label(reason)})


def _key_slot(keys: dict, url: str, site: str, profile: str,
              legacy_key: str) -> dict:
    slot = keys.get(url)
    if slot is None:
        slot = {"key": url, "url": url, "site": site, "profile": profile,
                "legacy_key": legacy_key, "catalog": [], "sitemap": [],
                "raw_forms": [], "notes": []}
        keys[url] = slot
    return slot


def resolve_profiles(profiles: dict[str, str] | None) -> dict[str, str]:
    """调用方没指定时走库里的按站档案；这里只做兜底，读库在 m5_batches。"""
    base = dict(config.SITE_PROFILE_DEFAULTS)
    if profiles:
        base.update({k: v for k, v in profiles.items() if k in config.SITE_BY_CODE})
    return base


def prepare(catalog_rows: list[dict], sitemap_rows: list[dict],
            profiles: dict[str, str] | None = None) -> dict:
    site_profiles = resolve_profiles(profiles)
    keys: dict[str, dict] = {}
    quarantine: list[dict] = []
    notes: list[dict] = []

    for row in catalog_rows:
        site = row["site"]
        profile = site_profiles.get(site) or config.DEFAULT_PROFILE
        norm = normalize.safe_normalize(row["raw_href"], row["page_url"], profile)
        if norm is None:
            _quarantine(quarantine, site, "catalog", row["line_no"], "BAD_URL",
                        row["raw_href"])
            continue
        if not scope.in_scope(norm.url):
            _quarantine(quarantine, site, "catalog", row["line_no"], "SCOPE_OFFSITE",
                        row["raw_href"])
            continue
        fields, attr_scheme = contract.map_attrs(row.get("attrs") or {})
        slot = _key_slot(keys, norm.url, scope.site_of(norm.url) or site, profile,
                         norm.legacy_key)
        entry = {"site": site, "key": norm.url, "url": norm.url,
                 "legacy_key": norm.legacy_key, "profile": profile,
                 "page_url": row["page_url"], "page_no": row.get("page_no", 1),
                 "line_no": row["line_no"], "raw_href": row["raw_href"],
                 "raw_text": row.get("raw_text", ""),
                 "entry_code": fields.get("entry_code", ""),
                 "dept_code": fields.get("dept_code", ""),
                 "attr_scheme": attr_scheme,
                 "changed": norm.changed, "notes": list(norm.notes)}
        slot["catalog"].append(entry)
        slot["raw_forms"].append(row["raw_href"])
        for note in norm.notes:
            if note not in slot["notes"]:
                slot["notes"].append(note)
        if attr_scheme == "legacy":
            _note(notes, site, "SCHEMA_LEGACY", f"旧属性映射：行 {row['line_no']}")

    for row in sitemap_rows:
        site = row["site"]
        profile = site_profiles.get(site) or config.DEFAULT_PROFILE
        norm = normalize.safe_normalize(row["raw_loc"], row["source_url"], profile)
        if norm is None:
            _quarantine(quarantine, site, "sitemap", row["row_no"], "BAD_URL",
                        row["raw_loc"])
            continue
        if not scope.in_scope(norm.url):
            _quarantine(quarantine, site, "sitemap", row["row_no"], "LOC_OFFSITE",
                        row["raw_loc"])
            continue
        if row.get("relative"):
            _note(notes, site, "LOC_RELATIVE", f"行 {row['row_no']} {row['raw_loc'][:60]}")
        slot = _key_slot(keys, norm.url, scope.site_of(norm.url) or site, profile,
                         norm.legacy_key)
        slot["sitemap"].append({"site": site, "key": norm.url, "url": norm.url,
                                "legacy_key": norm.legacy_key, "profile": profile,
                                "row_no": row["row_no"], "raw_loc": row["raw_loc"],
                                "lastmod": row.get("raw_lastmod", ""),
                                "changed": norm.changed, "notes": list(norm.notes)})
        slot["raw_forms"].append(row["raw_loc"])
        for note in norm.notes:
            if note not in slot["notes"]:
                slot["notes"].append(note)

    ordered = [keys[k] for k in sorted(keys)]
    entries = [entry for slot in ordered for entry in slot["catalog"]]
    entries.sort(key=lambda row: (row["site"], row["page_no"], row["line_no"]))
    return {"keys": ordered, "entries": entries, "quarantine": quarantine,
            "notes": notes, "key_count": len(ordered),
            "profiles": site_profiles}
