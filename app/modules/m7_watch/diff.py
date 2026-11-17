"""M7 规则二：两批次差异、回归清单与站点趋势。

三条规则都能单测，不依赖 HTTP，也不读库：

1. 变化分桶：上次状态 × 本次状态共 9 种组合，每种一个固定归属（TRANSITIONS）。
   上次死链、本次能打开但要跳转的记「已改善」——地址能打开了，不能记成仍未修复。
2. 基准不同（TRAP-28）：只在单侧出现的地址，若两侧原始写法相同、补全基准不同
   （目录页地址 vs sitemap 地址），先配成「基准不同」，不能算成一条新增加一条消失。
   绝对写法按原样解析，不受基准影响，所以不参与配对。
3. 跨版本（站点改了地址规则）：先按旧键把两把尺子对上，再按原始写法唯一对应；
   拆键、并键单列，无法唯一对应的整组记「待核实」，都不能冒充新增或修复。
4. 跳转环（TRAP-27）：环按环内成员集合合并，多个入口只报一次；一路都是新地址、
   只是超过跳转上限的记「链过长」，不是环（判定在 chain.analyze）。
"""
from __future__ import annotations

from ... import reasons
from . import chain

# 快照 schema：v1 只增字段不改语义（新增 profiles / legacy_key），旧文件仍可读
SNAPSHOT_SCHEMA = "sitemapaudit/watch/1"

STATE_OK = "ok"
STATE_REDIRECT = "redirect"
STATE_DEAD = "dead"
STATES = (STATE_OK, STATE_REDIRECT, STATE_DEAD)
STATE_LABELS = {STATE_OK: "可达", STATE_REDIRECT: "跳转", STATE_DEAD: "死链"}
STATE_RANK = {STATE_OK: 0, STATE_REDIRECT: 1, STATE_DEAD: 2}

# 跨版本比较的配对依据：旧键、新键、两侧都有、无法唯一对应
MATCH_EXACT = "exact"          # 同一个键（同档案同口径）
MATCH_LEGACY = "legacy"        # 旧批用旧键、新批按新键，经旧键对应上
MATCH_RAW = "raw"              # 键变了，但同一原始写法唯一对应
MATCH_AMBIGUOUS = "ambiguous"  # 拆键/并键后多对一或一对多，不能唯一对应
MATCH_UNMATCHED = "unmatched"  # 本侧独有，另一侧没有任何依据
MATCH_LABELS = {
    MATCH_EXACT: "同键",
    MATCH_LEGACY: "旧键对应",
    MATCH_RAW: "原始写法对应",
    MATCH_AMBIGUOUS: "待核实",
    MATCH_UNMATCHED: "单侧",
    "resplit": "规则拆键",
    "remerged": "规则并键",
}
# 能唯一对应的三种配对依据；只有它们参与三态归属（修复/恶化/未变）
CONFIDENT_MATCHES = (MATCH_EXACT, MATCH_LEGACY, MATCH_RAW)

# 档案不同导致的「同址拆键 / 不同址并键」：单独成桶，不冒充新增或修复
SPLIT_BUCKET = "resplit"
SPLIT_BUCKET_MERGED = "remerged"

# 上次状态 × 本次状态 → 分桶。9 格全在表里，没有默认值，新增状态就必须补表。
TRANSITIONS: dict[tuple[str, str], str] = {
    (STATE_OK, STATE_OK): "unchanged",
    (STATE_OK, STATE_REDIRECT): "new_redirect",
    (STATE_OK, STATE_DEAD): "new_dead",
    (STATE_REDIRECT, STATE_OK): "recovered",
    (STATE_REDIRECT, STATE_REDIRECT): "still_redirect",
    (STATE_REDIRECT, STATE_DEAD): "new_dead",
    (STATE_DEAD, STATE_OK): "recovered",
    (STATE_DEAD, STATE_REDIRECT): "improved",
    (STATE_DEAD, STATE_DEAD): "still_dead",
}

# 缺陷/变更标签的 CSS 类名：error 用 bad，warn 用 warn，ok 用 ok
LEVEL_CLASS = {"error": "bad", "warn": "warn", "ok": "ok", "info": "info"}


def state_of(row: dict) -> str:
    """一条探测结果归入三态：状态码 0 与 4xx/5xx 都是死链，其余看有没有跳转。"""
    status = int(row.get("status") or 0)
    if status == 0 or status >= 400:
        return STATE_DEAD
    return STATE_REDIRECT if int(row.get("hop_count") or 0) > 0 else STATE_OK


def bucket_of(prev_state: str, cur_state: str) -> str:
    try:
        return TRANSITIONS[(prev_state, cur_state)]
    except KeyError as exc:
        raise ValueError(f"未登记的状态组合 {prev_state} → {cur_state}") from exc


def _chain_brief(info: dict) -> dict:
    return {"kind": info.get("kind", ""), "label": info.get("label", ""),
            "length": int(info.get("length") or 0),
            "members": list(info.get("members") or []),
            "loop_path": list(info.get("loop_path") or [])}


def snapshot(run: dict, note: str = "") -> dict:
    """把一次巡检压成可比对的快照：一条比较键一行，三态 + 跳转链摘要 + 原始写法。

    比较键用该批入库时的地址档案（run["profiles"]）真实算出的键，不是事后重算。
    ``legacy_key`` 是按旧档案算出的旧键：跨版本比较时用它找到「同一个地址」，
    但从不拿它覆盖 ``key``，所以拆分后的新键不会被伪装成旧键。
    """
    raws = chain.raw_index(run)
    profiles = run.get("profiles") or {}
    merged: dict[str, dict] = {}
    for probe_row in run.get("probes") or []:
        site = probe_row.get("site") or ""
        profile = probe_row.get("profile") or profiles.get(site) or ""
        key = chain.key_for(probe_row.get("key") or probe_row.get("url") or "", profile)
        if not key:
            continue
        # legacy_key 一律用旧口径：它只用于跨版本找「同一个地址」
        raw_key = probe_row.get("key") or probe_row.get("url") or ""
        legacy = probe_row.get("legacy_key") or raw_key
        item = {
            "key": key,
            "legacy_key": chain.canonical_input(legacy),
            "site": site,
            "profile": profile,
            "url": probe_row.get("url") or key,
            "status": int(probe_row.get("status") or 0),
            "hop_count": int(probe_row.get("hop_count") or 0),
            "category": probe_row.get("category") or "",
            "final_url": probe_row.get("final_url") or "",
            "chain": _chain_brief(chain.analyze(probe_row.get("hops"),
                                                probe_row.get("status") or 0)),
            "raws": list(raws.get(key) or []),
        }
        kept = merged.get(key)
        if kept is None or STATE_RANK[state_of(item)] > STATE_RANK[state_of(kept)]:
            merged[key] = item
    rows = [merged[key] for key in sorted(merged)]
    sites = list(run.get("sites") or [])
    return {
        "schema": SNAPSHOT_SCHEMA,
        "run_id": run.get("run_id", ""),
        "started_at": run.get("started_at", ""),
        "finished_at": run.get("finished_at", ""),
        "rule_version": run.get("rule_version", ""),
        "profiles": {site: profiles.get(site, "") for site in sites} if profiles else {},
        "sites": sites,
        "note": note,
        "row_count": len(rows),
        "rows": rows,
    }


def index(snapshot_doc: dict) -> dict[str, dict]:
    """按比较键建索引；导入的快照写法不统一时也先归一再配对，重复键留较差的那条。

    快照行的键按其站点档案重新算（快照里的 key 可能来自不同版本），
    legacy_key 永远按旧口径；v1 快照没有 profile 时当作旧口径。
    """
    out: dict[str, dict] = {}
    profiles = _profile_map(snapshot_doc)
    for row in snapshot_doc.get("rows") or []:
        site = row.get("site") or ""
        profile = row.get("profile") or profiles.get(site) or ""
        original = row.get("key") or row.get("url") or ""
        key = chain.key_for(original, profile)
        if not key:
            continue
        item = dict(row)
        item["key"] = key
        item["profile"] = profile
        item["legacy_key"] = chain.canonical_input(
            row.get("legacy_key") or original)
        kept = out.get(key)
        if kept is None or STATE_RANK[state_of(item)] > STATE_RANK[state_of(kept)]:
            out[key] = item
    return out


def _raw_index(rows: dict[str, dict], keys) -> dict[str, list[tuple[str, dict]]]:
    out: dict[str, list[tuple[str, dict]]] = {}
    for key in keys:
        for item in rows[key].get("raws") or []:
            raw = (item.get("raw") or "").strip()
            if raw:
                out.setdefault(raw.lower(), []).append((key, item))
    return out


def _is_relative(raw: str) -> bool:
    text = (raw or "").strip().lower()
    return bool(text) and not text.startswith(("http://", "https://"))


def _base_differs(left: dict, right: dict) -> bool:
    return ((left.get("base_kind") or "") != (right.get("base_kind") or "")
            or (left.get("base") or "") != (right.get("base") or ""))


def basis_pairs(prev_index: dict[str, dict], cur_index: dict[str, dict],
                removed: list[str], added: list[str]) -> tuple[list[dict], list[str], list[str]]:
    """把「同写法、不同补全基准」的消失/新增配成一对，剩下的才计入增删。"""
    cur_raws = _raw_index(cur_index, added)
    used_prev: set[str] = set()
    used_cur: set[str] = set()
    pairs: list[dict] = []
    for key in sorted(removed):
        for item in prev_index[key].get("raws") or []:
            raw = (item.get("raw") or "").strip()
            if not _is_relative(raw):
                continue
            matched = False
            for other_key, other in cur_raws.get(raw.lower(), []):
                if other_key in used_cur or not _base_differs(item, other):
                    continue
                used_prev.add(key)
                used_cur.add(other_key)
                pairs.append({"prev_key": key, "cur_key": other_key,
                              "raw": raw, "prev_base": item.get("base") or "",
                              "cur_base": other.get("base") or ""})
                matched = True
                break
            if matched:
                break
    left_removed = [key for key in removed if key not in used_prev]
    left_added = [key for key in added if key not in used_cur]
    return pairs, left_removed, left_added


def _row(key: str, bucket: str, prev: dict | None, cur: dict | None,
         prev_state: str, cur_state: str, match: str = "") -> dict:
    source = cur or prev or {}
    return {
        "key": key,
        "bucket": bucket,
        "bucket_label": reasons.change_label(bucket),
        "level": LEVEL_CLASS.get(reasons.change_level(bucket), "info"),
        "note": reasons.change_note(bucket),
        "site": source.get("site") or "",
        "url": source.get("url") or key,
        "status_prev": (prev or {}).get("status", "—") if prev else "—",
        "status_cur": (cur or {}).get("status", "—") if cur else "—",
        "prev_state": prev_state,
        "cur_state": cur_state,
        "prev_state_label": STATE_LABELS.get(prev_state, "—"),
        "cur_state_label": STATE_LABELS.get(cur_state, "—"),
        "prev_key": (prev or {}).get("key", "") if prev else "",
        "prev_legacy_key": (prev or {}).get("legacy_key", "") if prev else "",
        "cur_legacy_key": (cur or {}).get("legacy_key", "") if cur else "",
        "match": match,
        "match_label": MATCH_LABELS.get(match, MATCH_LABELS[MATCH_UNMATCHED]),
        "chain": (cur or prev or {}).get("chain") or {},
    }


def _profile_map(doc: dict) -> dict[str, str]:
    profiles = dict(doc.get("profiles") or {})
    for row in doc.get("rows") or []:
        site = row.get("site") or ""
        if site and site not in profiles and row.get("profile"):
            profiles[site] = row["profile"]
    return profiles


def _site_changed(prev_doc: dict, cur_doc: dict) -> dict[str, bool]:
    """各站两次快照的规则档案是否不同；v1 快照没有 profiles 时按旧口径推断。"""
    prev = _profile_map(prev_doc)
    cur = _profile_map(cur_doc)
    sites = set(prev) | set(cur)
    for row in (prev_doc.get("rows") or []) + (cur_doc.get("rows") or []):
        if row.get("site"):
            sites.add(row["site"])
    out = {}
    for site in sites:
        before = prev.get(site) or "legacy"
        after = cur.get(site) or "legacy"
        out[site] = before != after
    return out


def _legacy_groups(rows: dict[str, dict]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for key, row in rows.items():
        groups.setdefault(row.get("legacy_key") or key, []).append(key)
    for value in groups.values():
        value.sort()
    return groups


def _match_site(prev_rows: dict[str, dict], cur_rows: dict[str, dict],
                profile_changed: bool):
    """一站之内的配对：同键 → 旧键 / 原始写法；拆键、并键、多义单列。

    返回 (pairs, prev_only, cur_only)。pairs 元素带 kind：
    exact / legacy / raw / resplit / remerged / ambiguous / basis。

    档案没变时键就是同一把尺子，只在「同写法不同补全基准」上配 basis；
    档案变了才用旧键把两把尺子对上，并对多对一/一对多给出明确归属。
    """
    pairs: list[dict] = []
    consumed_prev: set[str] = set()
    consumed_cur: set[str] = set()

    if not profile_changed:
        # 同一把尺子：同键直接对上，其余只处理补全基准不同的同写法（TRAP-28 原语义）
        for key in sorted(set(prev_rows) & set(cur_rows)):
            pairs.append({"kind": MATCH_EXACT, "prev_key": key, "cur_key": key})
            consumed_prev.add(key)
            consumed_cur.add(key)
        prev_only = [key for key in prev_rows if key not in consumed_prev]
        cur_only = [key for key in cur_rows if key not in consumed_cur]
        basis = _basis_pairs_disjoint(prev_rows, cur_rows, prev_only, cur_only)
        pairs.extend(basis)
        used_prev = {pair["prev_key"] for pair in basis}
        used_cur = {pair["cur_key"] for pair in basis}
        left_prev = [key for key in prev_only if key not in used_prev]
        left_cur = [key for key in cur_only if key not in used_cur]
        return pairs, left_prev, left_cur, profile_changed

    # 档案变化：先用旧键把两把尺子分到同一组，再判断一对一 / 拆键 / 并键 / 多义
    prev_groups = _legacy_groups(prev_rows)
    cur_groups = _legacy_groups(cur_rows)
    for legacy in sorted(set(prev_groups) | set(cur_groups)):
        before = prev_groups.get(legacy, [])
        after = cur_groups.get(legacy, [])
        if not before or not after:
            continue
        if len(before) == 1 and len(after) == 1:
            pkey, ckey = before[0], after[0]
            pairs.append({"kind": MATCH_EXACT if pkey == ckey else MATCH_LEGACY,
                          "prev_key": pkey, "cur_key": ckey, "legacy": legacy})
            consumed_prev.add(pkey)
            consumed_cur.add(ckey)
            continue
        if len(before) < len(after):
            # 旧口径一个键，新口径拆成多个：能唯一认出「原地址的延续」才保留对比，
            # 否则整组无法唯一对应，全部待核实（不能拿其中一条冒充修复）
            base = before[0]
            anchor = _split_anchor(prev_rows[base], base, after, cur_rows)
            if anchor is None:
                for ckey in after:
                    pairs.append({"kind": MATCH_AMBIGUOUS, "prev_key": base,
                                  "cur_key": ckey, "legacy": legacy,
                                  "siblings": list(after)})
                    consumed_cur.add(ckey)
                consumed_prev.add(base)
                continue
            for ckey in after:
                if ckey == anchor:
                    kind = MATCH_EXACT if base == ckey else MATCH_LEGACY
                    pairs.append({"kind": kind, "prev_key": base,
                                  "cur_key": ckey, "legacy": legacy})
                else:
                    pairs.append({"kind": SPLIT_BUCKET, "prev_key": base,
                                  "cur_key": ckey, "legacy": legacy,
                                  "siblings": list(after)})
                consumed_cur.add(ckey)
            consumed_prev.add(base)
            continue
        if len(after) < len(before):
            # 旧口径多条，新口径并回一条：同样先找唯一延续，否则整组待核实
            base = after[0]
            anchor = _merge_anchor(cur_rows[base], base, before, prev_rows)
            if anchor is None:
                for pkey in before:
                    pairs.append({"kind": MATCH_AMBIGUOUS, "prev_key": pkey,
                                  "cur_key": base, "legacy": legacy,
                                  "siblings": list(before)})
                    consumed_prev.add(pkey)
                consumed_cur.add(base)
                continue
            for pkey in before:
                if pkey == anchor:
                    kind = MATCH_EXACT if pkey == base else MATCH_LEGACY
                    pairs.append({"kind": kind, "prev_key": pkey,
                                  "cur_key": base, "legacy": legacy})
                else:
                    pairs.append({"kind": SPLIT_BUCKET_MERGED, "prev_key": pkey,
                                  "cur_key": base, "legacy": legacy,
                                  "siblings": list(before)})
                consumed_prev.add(pkey)
            consumed_cur.add(base)
            continue
        # 多对多：无法唯一对应，整组待核实
        for pkey in before:
            pairs.append({"kind": MATCH_AMBIGUOUS, "prev_key": pkey,
                          "cur_key": "", "legacy": legacy,
                          "siblings": list(after)})
            consumed_prev.add(pkey)
        for ckey in after:
            pairs.append({"kind": MATCH_AMBIGUOUS, "prev_key": "",
                          "cur_key": ckey, "legacy": legacy,
                          "siblings": list(before)})
            consumed_cur.add(ckey)

    prev_only = [key for key in prev_rows if key not in consumed_prev]
    cur_only = [key for key in cur_rows if key not in consumed_cur]

    # 3. 档案变化但仍未对上：同一原始写法在两侧都唯一出现，就是同一地址的依据
    raw_prev: dict[str, list[str]] = {}
    raw_cur: dict[str, list[str]] = {}
    for key in prev_only:
        for item in prev_rows[key].get("raws") or []:
            raw = (item.get("raw") or "").strip().lower()
            if raw:
                raw_prev.setdefault(raw, []).append(key)
    for key in cur_only:
        for item in cur_rows[key].get("raws") or []:
            raw = (item.get("raw") or "").strip().lower()
            if raw:
                raw_cur.setdefault(raw, []).append(key)
    used_prev: set[str] = set()
    used_cur: set[str] = set()
    for raw in sorted(set(raw_prev) & set(raw_cur)):
        left = [key for key in raw_prev[raw] if key not in used_prev]
        right = [key for key in raw_cur[raw] if key not in used_cur]
        if len(left) == 1 and len(right) == 1:
            pairs.append({"kind": MATCH_RAW, "prev_key": left[0],
                          "cur_key": right[0], "raw": raw})
            used_prev.add(left[0])
            used_cur.add(right[0])
        else:
            for key in left:
                pairs.append({"kind": MATCH_AMBIGUOUS, "prev_key": key,
                              "cur_key": "", "raw": raw, "siblings": right})
                used_prev.add(key)
            for key in right:
                pairs.append({"kind": MATCH_AMBIGUOUS, "prev_key": "",
                              "cur_key": key, "raw": raw, "siblings": left})
                used_cur.add(key)
    left_prev = [key for key in prev_only if key not in used_prev]
    left_cur = [key for key in cur_only if key not in used_cur]
    return pairs, left_prev, left_cur, profile_changed


def _raw_set(row: dict | None) -> set[str]:
    return {(item.get("raw") or "").strip().lower()
            for item in (row or {}).get("raws") or [] if (item.get("raw") or "").strip()}


def _split_anchor(prev_row: dict, base: str, after: list[str],
                  cur_rows: dict[str, dict]) -> str | None:
    """拆键组里唯一能认出「原地址延续」的那条；认不出返回 None（整组待核实）。

    先看同键：新键里保留了旧键字面的那一条就是延续。
    再看原始写法：旧行的写法只在新组里恰好一条上出现，才算唯一对应。
    """
    if base in after:
        return base
    raws = _raw_set(prev_row)
    if not raws:
        return None
    hits = [key for key in after if raws & _raw_set(cur_rows.get(key))]
    return hits[0] if len(hits) == 1 else None


def _merge_anchor(cur_row: dict, base: str, before: list[str],
                  prev_rows: dict[str, dict]) -> str | None:
    """并键组里旧侧唯一延续的那条；同键优先，其次原始写法唯一命中。"""
    if base in before:
        return base
    raws = _raw_set(cur_row)
    if not raws:
        return None
    hits = [key for key in before if raws & _raw_set(prev_rows.get(key))]
    return hits[0] if len(hits) == 1 else None


def _basis_pairs_disjoint(prev_rows, cur_rows, prev_only, cur_only) -> list[dict]:
    if not prev_only or not cur_only:
        return []
    prev_index = {key: prev_rows[key] for key in prev_only}
    cur_index = {key: cur_rows[key] for key in cur_only}
    basis, _left_prev, _left_cur = basis_pairs(prev_index, cur_index,
                                               list(prev_index), list(cur_index))
    return [{"kind": "basis", "prev_key": pair["prev_key"],
             "cur_key": pair["cur_key"], "raw": pair["raw"],
             "prev_base": pair["prev_base"], "cur_base": pair["cur_base"]}
            for pair in basis]


def compare(prev_snapshot: dict, cur_snapshot: dict) -> dict:
    """两次快照配对：先按旧键找同一地址，再给出对应依据；多义一律待核实。

    同档案时旧键与新键一致，行为与逐键比较完全一样；
    跨版本（某站切了新档案）时，拆键、并键和无法唯一对应的行单列，不冒充新增/修复。
    """
    prev_index = index(prev_snapshot)
    cur_index = index(cur_snapshot)
    changed = _site_changed(prev_snapshot, cur_snapshot)

    sites = sorted({row.get("site") or "" for row in
                    list(prev_index.values()) + list(cur_index.values())})
    rows: list[dict] = []
    for site in sites:
        prev_rows = {key: row for key, row in prev_index.items()
                     if (row.get("site") or "") == site}
        cur_rows = {key: row for key, row in cur_index.items()
                    if (row.get("site") or "") == site}
        pairs, prev_only, cur_only, profile_changed = _match_site(
            prev_rows, cur_rows, changed.get(site, False))

        for pair in pairs:
            prev = prev_rows.get(pair["prev_key"]) if pair["prev_key"] else None
            cur = cur_rows.get(pair["cur_key"]) if pair["cur_key"] else None
            kind = pair["kind"]
            if kind == "basis":
                prev_state, cur_state = state_of(prev), state_of(cur)
                row = _row(cur["key"], "basis", prev, cur, prev_state, cur_state,
                           MATCH_RAW)
            elif kind in (MATCH_EXACT, MATCH_LEGACY, MATCH_RAW):
                prev_state, cur_state = state_of(prev), state_of(cur)
                row = _row(cur["key"], bucket_of(prev_state, cur_state), prev, cur,
                           prev_state, cur_state, kind)
            elif kind == "resplit":
                prev_state, cur_state = state_of(prev), state_of(cur)
                row = _row(cur["key"], SPLIT_BUCKET, prev, cur,
                           prev_state, cur_state, kind)
                row["note"] = "旧规则合并的一条地址，新规则下分成多条；逐条对比，不计新增与修复"
            elif kind == "remerged":
                prev_state, cur_state = state_of(prev), state_of(cur)
                row = _row(cur["key"], SPLIT_BUCKET_MERGED, prev, cur,
                           prev_state, cur_state, kind)
                row["note"] = "旧规则下多条地址，新规则合并为一条；不计消失与修复"
            else:
                prev_state = state_of(prev) if prev else ""
                cur_state = state_of(cur) if cur else ""
                row = _row((cur or prev or {}).get("key", ""), "unverified",
                           prev, cur, prev_state, cur_state, MATCH_AMBIGUOUS)
                row["note"] = "无法唯一对应，需人工核实；不计入新增、消失与修复"
            if pair.get("raw"):
                row["raw"] = pair["raw"]
            if pair.get("siblings"):
                row["siblings"] = pair["siblings"]
            if pair.get("legacy"):
                row["legacy_key"] = pair["legacy"]
            if pair.get("prev_base") is not None:
                row["prev_base"] = pair["prev_base"]
                row["cur_base"] = pair["cur_base"]
            if row.get("key"):
                rows.append(row)

        for key in prev_only:
            prev = prev_rows[key]
            if profile_changed:
                row = _row(key, "unverified", prev, None, state_of(prev), "",
                           MATCH_UNMATCHED)
                row["note"] = "站点规则已切换，本次没有唯一对应地址，需人工核实"
            else:
                row = _row(key, "removed", prev, None, state_of(prev), "",
                           MATCH_UNMATCHED)
            rows.append(row)
        for key in cur_only:
            cur = cur_rows[key]
            if profile_changed:
                row = _row(key, "unverified", None, cur, "", state_of(cur),
                           MATCH_UNMATCHED)
                row["note"] = "站点规则已切换，本次没有唯一对应地址，需人工核实"
            else:
                row = _row(key, "added", None, cur, "", state_of(cur),
                           MATCH_UNMATCHED)
            rows.append(row)

    rows.sort(key=lambda row: (reasons.change_rank(row["bucket"]),
                               row.get("site") or "", row["key"]))
    return {"rows": rows, "summary": summarize(rows),
            "prev_run_id": prev_snapshot.get("run_id", ""),
            "cur_run_id": cur_snapshot.get("run_id", ""),
            "profile_changes": {site: True for site, flag in changed.items() if flag}}


def regressions(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row["bucket"] in reasons.ACTIONABLE_BUCKETS]


def summarize(rows: list[dict]) -> dict:
    counts = {bucket: 0 for bucket in reasons.CHANGE_ORDER}
    by_site: dict[str, dict] = {}
    for row in rows:
        counts[row["bucket"]] = counts.get(row["bucket"], 0) + 1
        site = row.get("site") or "—"
        slot = by_site.setdefault(site, {"site": site, "total": 0})
        slot["total"] += 1
        slot[row["bucket"]] = slot.get(row["bucket"], 0) + 1
    buckets = [{"key": bucket, "label": reasons.change_label(bucket),
                "level": LEVEL_CLASS.get(reasons.change_level(bucket), "info"),
                "count": counts[bucket]} for bucket in reasons.CHANGE_ORDER]
    return {"total": len(rows), "bucket_counts": counts, "buckets": buckets,
            "actionable": len(regressions(rows)),
            "by_site": [by_site[key] for key in sorted(by_site)]}


def trend(runs: list[dict]) -> dict:
    """按站点看三态计数随批次的变化；runs 按时间升序传入。"""
    rows: list[dict] = []
    sites: set[str] = set()
    for run in runs:
        doc = snapshot(run)
        counts: dict[str, dict[str, int]] = {}
        for row in doc["rows"]:
            site = row.get("site") or "—"
            sites.add(site)
            slot = counts.setdefault(site, {STATE_OK: 0, STATE_REDIRECT: 0, STATE_DEAD: 0})
            slot[state_of(row)] += 1
        rows.append({"run_id": doc["run_id"], "started_at": doc["started_at"],
                     "total": doc["row_count"], "sites": counts})
    return {"runs": rows, "sites": sorted(sites),
            "states": list(STATES), "state_labels": dict(STATE_LABELS)}
