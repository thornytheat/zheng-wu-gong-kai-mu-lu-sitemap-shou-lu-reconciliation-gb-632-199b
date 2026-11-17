"""地址规范化：7 步固定顺序，按站点选用的规则档案输出比较键。

顺序本身是规则的一部分（先补全相对地址才能取主机，先丢跟踪参数才能排序），
所以 normalize() 里每一步都写成显式函数并按固定次序调用，不允许按 dict 遍历顺序拼。
改写痕迹存进 notes，缺陷分类的 CANONICAL_MISMATCH 依据就是它非空。

站点改版后，路径大小写与重复参数的先后顺序开始区分页面，于是同一套步骤有两种开关：

- legacy（旧口径）：路径整体小写、参数按名称+值排序，同上季度；
- strict（新口径）：路径保留原始大小写、参数保留原始顺序（含重复参数）。

档案只改变「算不算同一个地址」，不改变跟踪参数丢弃、片段丢弃、尾斜杠这些基础步骤。
无论用哪个档案，结果里都带 ``legacy_url``——按旧口径算出的旧键，历史批次复算和
跨版本比较靠它对应，不需要把新键伪造成旧键。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from ... import config

_DUP_SLASH_RE = re.compile(r"/{2,}")


@dataclass(frozen=True)
class RuleSet:
    path_case_sensitive: bool
    sort_query: bool


def rules_for(profile: str | None) -> RuleSet:
    entry = config.ADDRESS_PROFILES.get(profile or config.DEFAULT_PROFILE)
    if entry is None:
        raise ValueError(f"未知地址规则档案 {profile}")
    return RuleSet(path_case_sensitive=bool(entry["path_case_sensitive"]),
                   sort_query=bool(entry["sort_query"]))


@dataclass(frozen=True)
class Normalized:
    raw: str
    url: str
    scheme: str
    host: str
    path: str
    query: str
    notes: tuple[str, ...] = field(default_factory=tuple)
    profile: str = config.DEFAULT_PROFILE
    legacy_url: str = ""

    @property
    def key(self) -> str:
        return self.url

    @property
    def legacy_key(self) -> str:
        """旧口径下的键；档案本身就是 legacy 时与 url 相同。"""
        return self.legacy_url or self.url

    @property
    def changed(self) -> bool:
        return bool(self.notes)


def _strip_scheme(text: str) -> str:
    lowered = text.lower()
    if lowered.startswith(config.SCRIPT_SCHEMES):
        raise ValueError("地址不是网页地址")
    if text.startswith("//"):
        return "http:" + text
    return text


def _clean_query(query: str, rules: RuleSet) -> tuple[str, list[str], bool]:
    notes: list[str] = []
    if not query:
        return "", notes, False
    pairs = parse_qsl(query, keep_blank_values=True)
    kept = []
    dropped_tracking = False
    for name, value in pairs:
        if name.lower() in config.TRACKING_PARAMS:
            dropped_tracking = True
            continue
        kept.append((name, value))
    if dropped_tracking:
        notes.append("tracking")
    if rules.sort_query:
        if [p[0] for p in kept] != sorted(p[0] for p in kept):
            notes.append("param_order")
        kept.sort()
    if len(kept) != len(pairs) and not dropped_tracking:
        notes.append("tracking")
    return urlencode(kept), notes, dropped_tracking


def _build(raw: str, base_url: str | None, rules: RuleSet,
           profile: str) -> Normalized:
    text = (raw or "").strip()
    if not text:
        raise ValueError("地址为空")
    notes: list[str] = []
    text = _strip_scheme(text)
    parts = urlsplit(text)
    if not parts.scheme:
        if not base_url:
            raise ValueError("地址缺少协议且没有基地址")
        text = urljoin(base_url, text)
        notes.append("relative")
        parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in config.ALLOWED_SCHEMES:
        raise ValueError(f"不接受的协议 {scheme}")
    if scheme != parts.scheme:
        notes.append("scheme")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("地址缺少主机")
    raw_host = parts.netloc.rsplit("@", 1)[-1].split(":")[0]
    if raw_host != host:
        notes.append("case")
    port = parts.port
    port_text = ""
    if port is not None:
        if config.DEFAULT_PORTS.get(scheme) == port:
            notes.append("default_port")
        else:
            port_text = f":{port}"
    path = parts.path or "/"
    if _DUP_SLASH_RE.search(path):
        path = _DUP_SLASH_RE.sub("/", path)
        notes.append("duplicate_slash")
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
        notes.append("trailing_slash")
    if not rules.path_case_sensitive:
        lowered_path = path.lower()
        if lowered_path != path:
            notes.append("case")
        path = lowered_path
    query, query_notes, _dropped = _clean_query(parts.query, rules)
    notes.extend(query_notes)
    if parts.fragment:
        notes.append("fragment")
    url = f"{scheme}://{host}{port_text}{path}" + (f"?{query}" if query else "")
    seen: list[str] = []
    for note in notes:
        if note not in seen:
            seen.append(note)
    return Normalized(raw=raw, url=url, scheme=scheme, host=host, path=path,
                      query=query, notes=tuple(seen), profile=profile,
                      legacy_url="")


def normalize(raw: str, base_url: str | None = None,
              profile: str | None = None) -> Normalized:
    """按站点档案规范化；结果始终附带旧口径键（legacy_url）。"""
    chosen = profile or config.DEFAULT_PROFILE
    rules = rules_for(chosen)
    result = _build(raw, base_url, rules, chosen)
    if chosen == config.PROFILE_LEGACY or rules == rules_for(config.PROFILE_LEGACY):
        legacy = result.url
    else:
        legacy = _build(raw, base_url, rules_for(config.PROFILE_LEGACY),
                        config.PROFILE_LEGACY).url
    return Normalized(raw=result.raw, url=result.url, scheme=result.scheme,
                      host=result.host, path=result.path, query=result.query,
                      notes=result.notes, profile=chosen, legacy_url=legacy)


def safe_normalize(raw: str, base_url: str | None = None,
                   profile: str | None = None) -> Normalized | None:
    try:
        return normalize(raw, base_url, profile)
    except ValueError:
        return None
