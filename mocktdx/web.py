"""Agent interface (TDNext-style), admin area, and the client portal (TDClient-style), plus the Web API mount."""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import api, db, settings as settings_module, tdx
from .seed import seed
from .util import configure_logging

log = logging.getLogger("mocktdx.web")
settings = settings_module.load()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    app.state.settings = settings
    conn = db.connect(settings.db_path)
    try:
        info = seed(conn, settings)
        if info.get("seeded"):
            log.info("seeded", extra={"extra": info})
    finally:
        conn.close()
    if not settings.web_services_key:
        log.warning("TDX_WEB_SERVICES_KEY is not set; loginadmin will refuse every call")
    yield


app = FastAPI(title="mock-tdx", lifespan=lifespan)
app.state.settings = settings
app.include_router(api.router)
app.middleware("http")(api.log_api_calls)


@app.exception_handler(tdx.TDXError)
async def _tdx_error(request: Request, exc: tdx.TDXError):
    if request.url.path.startswith("/TDWebApi/") or "text/html" not in request.headers.get("accept", ""):
        return api.error_response(exc)
    return _page(request, "error.html", _ctx(request, "", error=str(exc), code=exc.status), exc.status)


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    if "text/html" in request.headers.get("accept", "") and not request.url.path.startswith("/TDWebApi/"):
        return _page(request, "error.html", _ctx(request, "", error=exc.detail, code=exc.status_code), exc.status_code)
    return JSONResponse({"Message": exc.detail}, status_code=exc.status_code)


# ---------------------------------------------------------------- helpers

def _conn():
    return db.connect(settings.db_path)


def _acting(request: Request, conn):
    """The lab has no login screen: a cookie says which technician you are acting as."""
    uid = request.cookies.get("tdx_uid")
    row = tdx.person(conn, uid) if uid else None
    if not row:
        row = tdx.person_by(conn, UserName="dokafor") or conn.execute("SELECT * FROM people WHERE SecurityRoleName != 'Client' ORDER BY UserName LIMIT 1").fetchone()
    return row


def _ctx(request: Request, nav: str, conn=None, **kw) -> dict:
    ctx = {"request": request, "s": settings, "nav": nav, "msg": request.query_params.get("msg"), "app_id": settings.app_id}
    if conn is not None:
        ctx["me"] = _acting(request, conn)
        ctx["technicians"] = conn.execute("SELECT * FROM people WHERE SecurityRoleName != 'Client' AND IsActive = 1 ORDER BY LastName").fetchall()
        ctx["L"] = {"statuses": tdx.statuses(conn), "priorities": tdx.priorities(conn), "types": tdx.types(conn), "groups": tdx.groups(conn),
                    "accounts": tdx.accounts(conn), "forms": {f["ID"]: dict(f, Attributes=json.loads(f["Attributes"])) for f in tdx.forms(conn)},
                    "workflows": tdx.workflows(conn)}
    ctx.update(kw)
    return ctx


def _page(request: Request, name: str, ctx: dict, status_code: int = 200):
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _redirect(url: str, msg: str | None = None) -> RedirectResponse:
    if msg:
        url = f"{url}{'&' if '?' in url else '?'}msg={msg}"
    return RedirectResponse(url, status_code=303)


async def _form(request: Request) -> dict[str, str]:
    data = await request.form()
    return {k: (v.strip() if isinstance(v, str) else "") for k, v in data.items()}


def _attributes_from_form(form_def: dict | None, f: dict) -> list:
    out = []
    for a in (form_def or {"Attributes": []})["Attributes"]:
        value = f.get(f"attr_{a['ID']}", "")
        if a.get("IsRequired") and not value:
            raise tdx.TDXError(f"{a['Name']} is required")
        if value:
            out.append({"ID": a["ID"], "Value": value})
    return out


@app.get("/healthz")
def healthz():
    conn = _conn()
    try:
        return {"status": "ok", "tickets": conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0],
                "people": conn.execute("SELECT COUNT(*) FROM people").fetchone()[0], "api_key_set": bool(settings.web_services_key)}
    finally:
        conn.close()


@app.get("/")
def root():
    return RedirectResponse("/TDNext", status_code=307)


@app.post("/TDNext/switch-user")
async def switch_user(request: Request):
    f = await _form(request)
    resp = _redirect(f.get("next") or "/TDNext", "Now acting as the selected technician")
    resp.set_cookie("tdx_uid", f.get("uid", ""), httponly=True, samesite="lax")
    return resp


# ---------------------------------------------------------------- TDNext: desktop

@app.get("/TDNext", response_class=HTMLResponse)
def desktop(request: Request):
    conn = _conn()
    try:
        me = _acting(request, conn)
        ctx = _ctx(request, "desktop", conn, c=tdx.counts(conn, settings), work=tdx.my_work(conn, me["UID"]),
                   recent=tdx.list_tickets(conn, settings)[:10])
    finally:
        conn.close()
    return _page(request, "desktop.html", ctx)


# ---------------------------------------------------------------- TDNext: tickets

@app.get("/TDNext/Apps/{app_id}/Tickets", response_class=HTMLResponse)
def tickets(request: Request, app_id: int, view: str = "open", group: int | None = None, q: str | None = None):
    conn = _conn()
    try:
        rows = tdx.list_tickets(conn, settings, None if view == "all" else view, group, q)
        ctx = _ctx(request, "tickets", conn, rows=[tdx.ticket_json(conn, r) for r in rows], view=view, group=group, q=q or "")
    finally:
        conn.close()
    return _page(request, "tickets.html", ctx)


@app.get("/TDNext/Apps/{app_id}/Tickets/New", response_class=HTMLResponse)
def ticket_new(request: Request, app_id: int, type: int | None = None):
    conn = _conn()
    try:
        ctx = _ctx(request, "tickets", conn, type_id=type, people=conn.execute("SELECT * FROM people WHERE IsActive = 1 ORDER BY LastName, FirstName").fetchall())
    finally:
        conn.close()
    return _page(request, "ticket_new.html", ctx)


@app.post("/TDNext/Apps/{app_id}/Tickets/New")
async def ticket_create(request: Request, app_id: int):
    f = await _form(request)
    conn = _conn()
    try:
        me = _acting(request, conn)
        form_id = int(f["form_id"]) if f.get("form_id") else None
        form_def = tdx.form(conn, form_id) if form_id else None
        row = tdx.create_ticket(conn, settings, {
            "TypeID": int(f.get("type_id") or 0), "FormID": form_id, "Title": f.get("title", ""), "Description": f.get("description", ""),
            "PriorityID": int(f.get("priority_id") or 2), "RequestorUid": f.get("requestor_uid") or None,
            "ResponsibleGroupID": int(f["group_id"]) if f.get("group_id") else None, "Attributes": _attributes_from_form(form_def, f),
        }, me["UID"], source="TDNext")
    finally:
        conn.close()
    return _redirect(f"/TDNext/Apps/{app_id}/Tickets/{row['ID']}", f"Ticket {row['ID']} created")


@app.get("/TDNext/Apps/{app_id}/Tickets/{ticket_id}", response_class=HTMLResponse)
def ticket_detail(request: Request, app_id: int, ticket_id: int):
    conn = _conn()
    try:
        row = tdx.ticket(conn, ticket_id)
        ctx = _ctx(request, "tickets", conn, t=tdx.ticket_json(conn, row), tasks=[tdx.task_json(conn, x) for x in tdx.tasks(conn, ticket_id)],
                   feed=tdx.feed(conn, ticket_id), approvals=tdx.approvals(conn, ticket_id), wf=tdx.workflow_view(conn, ticket_id),
                   people=conn.execute("SELECT * FROM people WHERE IsActive = 1 ORDER BY LastName, FirstName").fetchall())
    finally:
        conn.close()
    return _page(request, "ticket.html", ctx)


@app.post("/TDNext/Apps/{app_id}/Tickets/{ticket_id}/comment")
async def ticket_comment(request: Request, app_id: int, ticket_id: int):
    f = await _form(request)
    conn = _conn()
    try:
        me = _acting(request, conn)
        tdx.comment(conn, settings, ticket_id, me["UID"], {"Comments": f.get("comments", ""), "NewStatusID": int(f["status_id"]) if f.get("status_id") else None,
                                                           "IsPrivate": f.get("private") == "on"})
    finally:
        conn.close()
    return _redirect(f"/TDNext/Apps/{app_id}/Tickets/{ticket_id}", "Update posted")


@app.post("/TDNext/Apps/{app_id}/Tickets/{ticket_id}/update")
async def ticket_update(request: Request, app_id: int, ticket_id: int):
    f = await _form(request)
    conn = _conn()
    try:
        me = _acting(request, conn)
        fields = {"PriorityID": int(f["priority_id"]) if f.get("priority_id") else None,
                  "ResponsibleGroupID": int(f["group_id"]) if f.get("group_id") else None,
                  "ResponsibleUid": f.get("responsible_uid") or None, "EndDate": f.get("end_date") or None}
        tdx.update_ticket_fields(conn, settings, ticket_id, fields, me["UID"])
    finally:
        conn.close()
    return _redirect(f"/TDNext/Apps/{app_id}/Tickets/{ticket_id}", "Ticket updated")


@app.post("/TDNext/Apps/{app_id}/Tickets/{ticket_id}/tasks")
async def ticket_add_task(request: Request, app_id: int, ticket_id: int):
    f = await _form(request)
    conn = _conn()
    try:
        me = _acting(request, conn)
        tdx.create_task(conn, ticket_id, {"Title": f.get("title", ""), "Description": f.get("description", ""),
                                          "ResponsibleGroupID": int(f["group_id"]) if f.get("group_id") else None, "EndDate": f.get("end_date") or None}, me["UID"])
    finally:
        conn.close()
    return _redirect(f"/TDNext/Apps/{app_id}/Tickets/{ticket_id}", "Task added")


@app.post("/TDNext/Apps/{app_id}/Tickets/{ticket_id}/tasks/{task_id}")
async def ticket_task_update(request: Request, app_id: int, ticket_id: int, task_id: int):
    f = await _form(request)
    conn = _conn()
    try:
        me = _acting(request, conn)
        tdx.update_task(conn, settings, ticket_id, task_id, {"PercentComplete": int(f.get("percent") or 0), "ResponsibleUid": f.get("responsible_uid") or None},
                        me["UID"], comment_text=f.get("comments", ""))
    finally:
        conn.close()
    return _redirect(f"/TDNext/Apps/{app_id}/Tickets/{ticket_id}", "Task updated")


@app.post("/TDNext/Apps/{app_id}/Tickets/{ticket_id}/approvals/{approval_id}")
async def ticket_approval(request: Request, app_id: int, ticket_id: int, approval_id: int):
    f = await _form(request)
    conn = _conn()
    try:
        me = _acting(request, conn)
        tdx.decide_approval(conn, settings, approval_id, me["UID"], f.get("decision") == "approve", f.get("comments", ""))
    finally:
        conn.close()
    return _redirect(f"/TDNext/Apps/{app_id}/Tickets/{ticket_id}", "Decision recorded")


@app.post("/TDNext/Apps/{app_id}/Tickets/{ticket_id}/workflow")
async def ticket_workflow(request: Request, app_id: int, ticket_id: int):
    f = await _form(request)
    conn = _conn()
    try:
        me = _acting(request, conn)
        tdx.start_workflow(conn, settings, ticket_id, int(f.get("workflow_id") or 0), me["UID"])
    finally:
        conn.close()
    return _redirect(f"/TDNext/Apps/{app_id}/Tickets/{ticket_id}", "Workflow started")


# ---------------------------------------------------------------- TDNext: people

@app.get("/TDNext/Apps/People", response_class=HTMLResponse)
def people(request: Request, q: str | None = None, inactive: int = 0):
    conn = _conn()
    try:
        rows = tdx.people_search(conn, {"SearchText": q or "", "IsActive": None if inactive else True, "MaxResults": 500})
        ctx = _ctx(request, "people", conn, rows=[tdx.person_json(conn, r) for r in rows], q=q or "", inactive=inactive, imports=tdx.imports(conn))
    finally:
        conn.close()
    return _page(request, "people.html", ctx)


@app.post("/TDNext/Apps/People/Import")
async def people_import(request: Request, file: UploadFile):
    text = (await file.read()).decode("utf-8-sig")
    conn = _conn()
    try:
        result = tdx.import_people_csv(conn, text, source=f"upload:{file.filename}")
    finally:
        conn.close()
    return _redirect("/TDNext/Apps/People", f"Import: {result['rows']} rows, {result['created']} created, {result['updated']} updated, {result['deactivated']} deactivated, {len(result['errors'])} errors")


@app.get("/TDNext/Apps/People/{uid}", response_class=HTMLResponse)
def person(request: Request, uid: str):
    conn = _conn()
    try:
        row = tdx.person(conn, uid)
        if not row:
            raise HTTPException(404, "Person not found")
        ctx = _ctx(request, "people", conn, p=tdx.person_json(conn, row),
                   tickets=[tdx.ticket_json(conn, r) for r in tdx.list_tickets(conn, settings, None, None, None, uid)[:20]])
    finally:
        conn.close()
    return _page(request, "person.html", ctx)


# ---------------------------------------------------------------- TDAdmin

@app.get("/TDAdmin", response_class=HTMLResponse)
def admin(request: Request, tab: str = "types"):
    conn = _conn()
    try:
        ctx = _ctx(request, "admin", conn, tab=tab, rules=tdx.rules(conn), services=tdx.services(conn), logs=tdx.api_logs(conn),
                   members={g["ID"]: [tdx.full_name(tdx.person(conn, m["UID"])) for m in conn.execute("SELECT UID FROM group_members WHERE GroupID = ?", (g["ID"],))] for g in tdx.groups(conn)},
                   integration_uid=tdx.INTEGRATION_UID)
    finally:
        conn.close()
    return _page(request, "admin.html", ctx)


# ---------------------------------------------------------------- TDClient (portal)

@app.get("/TDClient", response_class=HTMLResponse)
@app.get("/TDClient/Requests/ServiceCatalog", response_class=HTMLResponse)
def client_catalog(request: Request):
    conn = _conn()
    try:
        ctx = _ctx(request, "client", conn, services=tdx.services(conn))
    finally:
        conn.close()
    return _page(request, "client_services.html", ctx)


@app.get("/TDClient/Requests/ServiceDet", response_class=HTMLResponse)
def client_service(request: Request, ID: int):
    conn = _conn()
    try:
        svc = conn.execute("SELECT * FROM services WHERE ID = ?", (ID,)).fetchone()
        if not svc:
            raise HTTPException(404, "Service not found")
        ctx = _ctx(request, "client", conn, svc=svc, form=tdx.form(conn, svc["FormID"]) if svc["FormID"] else None)
    finally:
        conn.close()
    return _page(request, "client_service.html", ctx)


@app.post("/TDClient/Requests/ServiceDet")
async def client_request(request: Request):
    f = await _form(request)
    conn = _conn()
    try:
        me = _acting(request, conn)
        svc = conn.execute("SELECT * FROM services WHERE ID = ?", (int(f.get("service_id") or 0),)).fetchone()
        if not svc:
            raise HTTPException(404, "Service not found")
        form_def = tdx.form(conn, svc["FormID"]) if svc["FormID"] else None
        row = tdx.create_ticket(conn, settings, {"TypeID": svc["TypeID"], "FormID": svc["FormID"], "ServiceID": svc["ID"],
                                                 "Title": f.get("title") or svc["Name"], "Description": f.get("description", ""),
                                                 "RequestorUid": me["UID"], "Attributes": _attributes_from_form(form_def, f)}, me["UID"], source="Portal")
    finally:
        conn.close()
    return _redirect(f"/TDClient/Requests/TicketRequests/TicketDet?TicketID={row['ID']}", f"Request {row['ID']} submitted")


@app.get("/TDClient/Requests/TicketRequests", response_class=HTMLResponse)
def client_requests(request: Request):
    conn = _conn()
    try:
        me = _acting(request, conn)
        ctx = _ctx(request, "client", conn, rows=[tdx.ticket_json(conn, r) for r in tdx.list_tickets(conn, settings, None, None, None, me["UID"])])
    finally:
        conn.close()
    return _page(request, "client_requests.html", ctx)


@app.get("/TDClient/Requests/TicketRequests/TicketDet", response_class=HTMLResponse)
def client_ticket(request: Request, TicketID: int):
    conn = _conn()
    try:
        row = tdx.ticket(conn, TicketID)
        ctx = _ctx(request, "client", conn, t=tdx.ticket_json(conn, row), feed=[x for x in tdx.feed(conn, TicketID) if not x["IsPrivate"]],
                   approvals=[a for a in tdx.approvals(conn, TicketID)], tasks=[tdx.task_json(conn, x) for x in tdx.tasks(conn, TicketID)])
    finally:
        conn.close()
    return _page(request, "client_ticket.html", ctx)


@app.post("/TDClient/Requests/TicketRequests/TicketDet")
async def client_ticket_action(request: Request):
    f = await _form(request)
    ticket_id = int(f.get("ticket_id") or 0)
    conn = _conn()
    try:
        me = _acting(request, conn)
        if f.get("approval_id"):
            tdx.decide_approval(conn, settings, int(f["approval_id"]), me["UID"], f.get("decision") == "approve", f.get("comments", ""))
            note = "Decision recorded"
        else:
            tdx.comment(conn, settings, ticket_id, me["UID"], {"Comments": f.get("comments", "")})
            note = "Comment added"
    finally:
        conn.close()
    return _redirect(f"/TDClient/Requests/TicketRequests/TicketDet?TicketID={ticket_id}", note)
