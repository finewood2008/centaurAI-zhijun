"""Box-hosted consent. The account service receives only opaque consent/grant metadata.

The cloud account adapter must implement the documented one-use ticket contract.
No desktop session, password, personal preview or material title crosses that adapter.
"""
from __future__ import annotations

import hashlib
import html
import secrets
import time
from urllib.parse import urlencode
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from .models import AccessError, GrantSpec, SECTIONS

TITLES = dict(zip(SECTIONS, ("我是谁", "我的人", "我的事", "我的原则", "我的做法", "我的方向")))
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
           "X-Content-Type-Options": "nosniff",
           "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"}


def page(content, status=200):
    return HTMLResponse("""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>知君 · 外部 Agent</title><style>body{margin:0;background:#f7f5ef;color:#29362d;font:16px/1.7 system-ui}
main{max-width:680px;margin:4vh auto;padding:28px}h1{font-size:26px}fieldset{border:1px solid #ccd2c9;border-radius:14px;padding:20px;margin:20px 0}
label{display:block;margin:10px 0}input{margin-right:8px}button,select{font:inherit;padding:9px 16px;border:1px solid #9aab98;border-radius:8px;background:white}
button[name=decision][value=approve]{background:#345841;color:white}small,p{color:#586354}.actions{display:flex;gap:12px;flex-wrap:wrap}</style><main>"""
        + content + "</main></html>", status_code=status, headers=HEADERS)


class ConsentBrowser:
    def __init__(self, gateway, account, origin, *, clock=time.time):
        self.gateway, self.account, self.origin, self.clock = gateway, account, origin, clock
        self.sessions = {}

    def _session(self, request):
        cookie = request.cookies.get("__Host-zhijun-consent", "")
        key = hashlib.sha256(cookie.encode()).hexdigest()
        session = self.sessions.get(key)
        if not session or session["expires"] <= self.clock():
            raise AccessError("CONSENT_EXPIRED", 401)
        return key, session

    async def start_request(self, request):
        # Cloud logs only opaque request IDs; state cookie binds the round trip.
        state = secrets.token_urlsafe(32)
        response = RedirectResponse(self.account.login_url(
            self.origin + "/external-agents/authorize", state, request.path_params["requestId"]), 303)
        response.set_cookie("__Host-zhijun-login", state, secure=True, httponly=True, samesite="lax", max_age=300)
        return response

    async def authorize(self, request):
        try:
            # Exchange is authenticated server-to-server and consumes ticket exactly once.
            ticket = request.query_params.get("ticket", "")
            if not ticket or len(ticket) > 512:
                raise AccessError()
            identity = await self.account.exchange(ticket)
            if identity.get("requestId"):
                state = request.cookies.get("__Host-zhijun-login", "")
                if not state or not secrets.compare_digest(state, identity.get("state", "")):
                    raise AccessError()
            if any(identity.get(k) != v for k, v in self.gateway.binding.model_dump().items()):
                raise AccessError()
            cookie, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > self.clock()}
            if len(self.sessions) >= 100:
                raise AccessError("CONSENT_LIMIT", 429)
            key = hashlib.sha256(cookie.encode()).hexdigest()
            session = {"identity": identity, "csrf": csrf, "expires": self.clock() + 300}
            self.sessions[key] = session
            response = await self.render(session)
            response.set_cookie("__Host-zhijun-consent", cookie, secure=True, httponly=True, samesite="strict", max_age=300)
            response.delete_cookie("__Host-zhijun-login", secure=True, httponly=True)
            return response
        except Exception:
            return page("<h1>授权暂时无法打开</h1><p>请回到 Agent 重新连接，或检查盒子是否在线。</p>", 403)

    async def render(self, session):
        identity = session["identity"]
        esc = lambda value: html.escape(str(value), quote=True)
        hidden = f'<input type="hidden" name="csrf" value="{esc(session["csrf"])}">'
        if identity.get("requestId"):
            pending = await self.gateway.owner("request_preview", {"requestId": identity["requestId"]})
            if pending["state"] != "pending":
                return page("<h1>这个请求已处理</h1><p>请回到 Agent 查看状态。</p>")
            materials = "".join(f'<li>{esc(m["title"])} · 版本 {esc(m["version"])}</li>' for m in pending.get("materials", []))
            previews = "".join(f'<p>{esc(p["text"])}</p>' for p in pending.get("previews", []))
            return page(f'<h1>确认资料交付</h1><p>{esc(pending["agentName"])} 的请求需要你确认。仅本次请求有效，资料版本变化后需要重新检索。</p>'
                f'<fieldset><legend>这次请求</legend><p>{esc(pending.get("query", ""))}</p><ul>{materials}</ul>{previews}</fieldset>'
                f'<form method="post" action="/external-agents/decision">{hidden}<div class="actions">'
                '<button name="decision" value="masked">脱敏后提供</button><button name="decision" value="original">允许本次原文</button>'
                '<button name="decision" value="cancel">取消</button></div></form>')
        preview = await self.gateway.owner("preview", {})
        session["preview"] = preview
        sections = "".join(f'<label><input type="checkbox" name="sections" value="{s}">{t}</label>' for s, t in TITLES.items())
        personal = "".join(f'<label><input type="checkbox" name="exclude" value="{esc(c["id"])}">排除：{esc(c["content"])}（{TITLES.get(c["section"], "")}）</label>' for c in preview["personal"])
        materials = "".join(f'<label><input type="checkbox" name="materials" value="{esc(m["id"])}">{esc(m["title"])} · 版本 {esc(m["version"])}</label>' for m in preview["materials"])
        return page(f'<h1>允许 {esc(identity["agentName"])} 读取哪些资料？</h1><p>调用方标识：{esc(identity["agentId"])}</p>'
            f'<form method="post" action="/external-agents/decision">{hidden}<fieldset><legend>个人信息</legend>{sections}'
            '<small>勾选类别后，授权期内该类别以后新增的合格信息也会提供。禁止外发、敏感和本地专用内容始终排除。</small>'
            f'<details><summary>查看当前可用内容，并排除个别条目</summary>{personal or "暂无可用内容"}</details>'
            '<label><input type="checkbox" name="legacy" value="yes">我确认将上述所选类别内的历史条目纳入此次授权；未勾选时只包含以后新增的合格信息。</label></fieldset>'
            f'<fieldset><legend>工作资料</legend>{materials or "暂无可用资料"}<small>只授权勾选的资料，新增资料不会自动开放。</small></fieldset>'
            '<label>授权期限 <select name="days"><option value="1">1 天</option><option value="7">7 天</option><option value="30" selected>30 天</option></select></label>'
            '<label><input type="checkbox" name="disclosure" value="yes" required>我理解内容会交给该 Agent 及其使用的处理服务；撤销可阻止后续读取，不能收回已交付内容。</label>'
            '<div class="actions"><button name="decision" value="approve">确认授权</button><button name="decision" value="cancel" formnovalidate>取消</button></div></form>')

    async def decide(self, request):
        key, session = None, None
        try:
            key, session = self._session(request)
            if request.headers.get("origin") != self.origin:
                raise AccessError()
            raw = await request.body()
            if len(raw) > 128000:
                raise AccessError()
            from urllib.parse import parse_qs
            form = parse_qs(raw.decode(), max_num_fields=12000)
            single = lambda name: form.get(name, [""])[0] if len(form.get(name, [])) <= 1 else ""
            if not secrets.compare_digest(single("csrf"), session["csrf"]):
                raise AccessError()
            # Consume before writes to make consent and sensitive release single use.
            self.sessions.pop(key, None)
            identity, decision = session["identity"], single("decision")
            if identity.get("requestId"):
                if decision not in ("masked", "original", "cancel"):
                    raise AccessError()
                await self.gateway.owner("decide", {"requestId": identity["requestId"], "decision": decision})
                return page("<h1>已处理</h1><p>可以关闭此页，回到 Agent 查看结果。</p>")
            grant = None
            if decision == "approve":
                sections, excluded = form.get("sections", []), form.get("exclude", [])
                legacy = [c["id"] for c in session["preview"]["personal"] if c["requiresLegacyConfirmation"]
                          and c["section"] in sections and c["id"] not in excluded] if single("legacy") == "yes" else []
                spec = GrantSpec(agentId=identity["agentId"], agentName=identity["agentName"], sections=sections,
                    materialIds=form.get("materials", []), excludedClaimIds=excluded, acknowledgedLegacyIds=legacy,
                    days=int(single("days")), disclosureAccepted=single("disclosure") == "yes")
                grant = await self.gateway.owner("create_grant", spec.model_dump())
            elif decision != "cancel":
                raise AccessError()
            try:
                # Cloud receives only identity-bound grant ID and expiry, no selections or previews.
                await self.account.complete(identity["consentId"], grant["id"] if grant else None,
                                            grant["expiresAt"] if grant else None)
            except Exception:
                if grant:
                    await self.gateway.owner("revoke_grant", {"grantId": grant["id"], "revision": grant["revision"]})
                raise
            return RedirectResponse(self.account.resume_url(identity["consentId"]), 303, headers=HEADERS)
        except Exception:
            return page("<h1>未完成授权</h1><p>请求已过期、范围发生变化或服务暂时不可用。请回到 Agent 重新发起。</p>", 409)

    def routes(self):
        return [Route("/external-agents/authorize", self.authorize),
                Route("/external-agents/decision", self.decide, methods=["POST"]),
                Route("/external-agents/requests/{requestId}", self.start_request)]
