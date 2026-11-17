"""M2 入口：契约查询、按站规则切换与规范化试算。

试算接口是给编辑用的：贴一个地址和站点（或档案），立刻看到比较键、旧口径的旧键
与改写痕迹，不必先跑整批巡检。非法地址返回 400，不做部分成功。

规则切换：
- GET  /api/rules              当前每站档案、可选档案与最近切换记录
- POST /api/rules/switch       切到新档案（校验 → 落库 → 记日志；失败保持旧档案）
- POST /api/rules/rollback     显式恢复某站到指定档案（默认回旧口径）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ... import config, errors, reasons
from ...web import Ctx, get_ctx, render
from . import contract, normalize, rules, scope

router = APIRouter(tags=["M2 URL 规则"])


@router.get("/api/contract")
def api_contract() -> dict:
    return contract.contract_payload()


@router.get("/api/rules")
def api_rules(ctx: Ctx = Depends(get_ctx)) -> dict:
    return rules.payload(ctx.store, ctx.settings)


@router.post("/api/rules/switch")
def api_switch(payload: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    sites = body.get("sites")
    if sites is not None and not isinstance(sites, (list, str)):
        raise errors.BadRequest("sites 必须是数组或站点代号", code="E_BAD_SITE")
    expected = body.get("expected_versions")
    if expected is not None and not isinstance(expected, dict):
        raise errors.BadRequest("expected_versions 必须是对象", code="E_BAD_REQUEST")
    result = rules.switch(ctx.store, ctx.settings, sites=sites,
                          profile=str(body.get("profile") or ""),
                          expected_versions=expected,
                          reason=str(body.get("reason") or ""))
    record = result["switch"]
    return {"status": record["status"], "reused": result["reused"],
            "switch": record, "detail": result["detail"],
            "sites": rules.payload(ctx.store, ctx.settings)["sites"]}


@router.post("/api/rules/rollback")
def api_rollback(payload: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    sites = body.get("sites")
    if not sites:
        raise errors.BadRequest("回滚需要 sites", code="E_BAD_SITE")
    target = str(body.get("profile") or config.PROFILE_LEGACY)
    result = rules.switch(ctx.store, ctx.settings, sites=sites, profile=target,
                          expected_versions=body.get("expected_versions"),
                          reason=str(body.get("reason") or "显式回滚"))
    record = result["switch"]
    return {"status": record["status"], "reused": result["reused"],
            "switch": record, "detail": result["detail"],
            "sites": rules.payload(ctx.store, ctx.settings)["sites"]}


@router.post("/api/url/normalize")
def api_normalize(payload: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    raw = body.get("url")
    if not isinstance(raw, str) or not raw.strip():
        raise errors.BadRequest("url 必填", code="E_BAD_URL")
    base = body.get("base")
    if base is not None and not isinstance(base, str):
        raise errors.BadRequest("base 必须是字符串", code="E_BAD_URL")
    profile = body.get("profile")
    site = body.get("site")
    if profile is not None and profile not in config.ADDRESS_PROFILES:
        raise errors.BadRequest(f"未知规则档案 {profile}", code="E_BAD_PROFILE")
    if site is not None and site not in config.SITE_BY_CODE:
        raise errors.BadRequest(f"未知站点 {site}", code="E_BAD_SITE")
    if profile is None and site:
        profile = rules.profiles_of(ctx.store, [site]).get(site)
    try:
        result = normalize.normalize(raw, base, profile)
    except ValueError as exc:
        raise errors.BadRequest(str(exc), code="E_BAD_URL") from exc
    return {
        "raw": result.raw,
        "url": result.url,
        "key": result.key,
        "legacy_key": result.legacy_key,
        "profile": result.profile,
        "profile_label": rules.profile_label(result.profile),
        "host": result.host,
        "path": result.path,
        "query": result.query,
        "changed": result.changed,
        "notes": [{"code": n, "label": reasons.note_label(n)} for n in result.notes],
        "site": scope.site_of(result.url),
        "in_scope": scope.in_scope(result.url),
    }


@router.get("/rules")
def page_rules(request: Request):
    ctx: Ctx = get_ctx(request)
    data = rules.payload(ctx.store, ctx.settings)
    return render(request, "rules.html", active="rules", data=data,
                  profiles=config.ADDRESS_PROFILES)
