"""The Web API, mounted at /TDWebApi/api like TeamDynamix. Shapes follow the TDX documentation closely enough
that a PowerShell client written here ports to a real tenant by changing the base URL and credentials.

Auth:   POST /auth {username,password} or POST /auth/loginadmin {BEID,WebServicesKey} -> JWT as text/plain
Then:   Authorization: Bearer <token>
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from . import db, tdx
from .settings import Settings

router = APIRouter(prefix="/TDWebApi/api")


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _conn(request: Request):
    return db.connect(_settings(request).db_path)


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise tdx.TDXError("Request body must be JSON")
    return body if isinstance(body, dict) else {"_list": body}


def _auth(request: Request) -> dict:
    return tdx.claims(_settings(request), request.headers.get("authorization"))


def _app(request: Request, app_id: int) -> None:
    if app_id != _settings(request).app_id:
        raise tdx.NotFound(f"Ticketing application {app_id} not found")


def error_response(exc: tdx.TDXError) -> JSONResponse:
    return JSONResponse({"Message": str(exc)}, status_code=exc.status)


# ---------------------------------------------------------------- auth

@router.post("/auth")
async def auth_user(request: Request):
    body = await _json(request)
    conn = _conn(request)
    try:
        token = tdx.login_user(conn, _settings(request), body.get("username", ""), body.get("password", ""))
    finally:
        conn.close()
    if not token:
        return JSONResponse({"Message": "Invalid username or password"}, status_code=401)
    return PlainTextResponse(token)


@router.post("/auth/loginadmin")
async def auth_admin(request: Request):
    body = await _json(request)
    token = tdx.login_admin(_settings(request), body.get("BEID", ""), body.get("WebServicesKey", ""))
    if not token:
        return JSONResponse({"Message": "Invalid BEID or WebServicesKey"}, status_code=401)
    return PlainTextResponse(token)


@router.get("/auth/getuser")
def auth_getuser(request: Request):
    who = _auth(request)
    conn = _conn(request)
    try:
        row = tdx.person(conn, who["uid"])
        return tdx.person_json(conn, row) if row else {"UID": who["uid"], "FullName": who["name"], "SecurityRoleName": "API"}
    finally:
        conn.close()


# ---------------------------------------------------------------- reference data

@router.get("/accounts")
def accounts(request: Request):
    _auth(request)
    conn = _conn(request)
    try:
        return [dict(r, IsActive=bool(r["IsActive"])) for r in tdx.accounts(conn)]
    finally:
        conn.close()


@router.get("/groups")
@router.post("/groups/search")
def groups(request: Request):
    _auth(request)
    conn = _conn(request)
    try:
        return [dict(r, IsActive=bool(r["IsActive"])) for r in tdx.groups(conn)]
    finally:
        conn.close()


@router.get("/{app_id}/tickets/statuses")
def statuses(request: Request, app_id: int):
    _auth(request); _app(request, app_id)
    conn = _conn(request)
    try:
        return [{"ID": r["ID"], "Name": r["Name"], "StatusClass": tdx.STATUS_CLASS[r["StatusClass"]], "Order": r["SortOrder"], "IsActive": True} for r in tdx.statuses(conn)]
    finally:
        conn.close()


@router.get("/{app_id}/tickets/priorities")
def priorities(request: Request, app_id: int):
    _auth(request); _app(request, app_id)
    conn = _conn(request)
    try:
        return [{"ID": r["ID"], "Name": r["Name"], "Order": r["SortOrder"]} for r in tdx.priorities(conn)]
    finally:
        conn.close()


@router.get("/{app_id}/tickets/types")
def types(request: Request, app_id: int):
    _auth(request); _app(request, app_id)
    conn = _conn(request)
    try:
        return [{"ID": r["ID"], "Name": r["Name"], "CategoryName": r["CategoryName"], "IsActive": True} for r in tdx.types(conn)]
    finally:
        conn.close()


@router.get("/{app_id}/tickets/forms")
def forms(request: Request, app_id: int):
    _auth(request); _app(request, app_id)
    conn = _conn(request)
    try:
        return [dict(f, Attributes=json.loads(f["Attributes"])) for f in tdx.forms(conn)]
    finally:
        conn.close()


# ---------------------------------------------------------------- tickets

@router.post("/{app_id}/tickets/search")
async def search(request: Request, app_id: int):
    _auth(request); _app(request, app_id)
    body = await _json(request)
    conn = _conn(request)
    try:
        return [tdx.ticket_json(conn, r) for r in tdx.search_tickets(conn, _settings(request), body)]
    finally:
        conn.close()


@router.post("/{app_id}/tickets", status_code=201)
async def create(request: Request, app_id: int):
    who = _auth(request); _app(request, app_id)
    body = await _json(request)
    conn = _conn(request)
    try:
        row = tdx.create_ticket(conn, _settings(request), body, who["uid"], source="Web Service")
        return tdx.ticket_json(conn, row)
    finally:
        conn.close()


@router.get("/{app_id}/tickets/{ticket_id}")
def get_ticket(request: Request, app_id: int, ticket_id: int):
    _auth(request); _app(request, app_id)
    conn = _conn(request)
    try:
        return tdx.ticket_json(conn, tdx.ticket(conn, ticket_id))
    finally:
        conn.close()


@router.patch("/{app_id}/tickets/{ticket_id}")
async def patch(request: Request, app_id: int, ticket_id: int):
    who = _auth(request); _app(request, app_id)
    body = await _json(request)
    ops = body.get("_list") if "_list" in body else [body]
    conn = _conn(request)
    try:
        return tdx.ticket_json(conn, tdx.patch_ticket(conn, _settings(request), ticket_id, ops, who["uid"]))
    finally:
        conn.close()


@router.get("/{app_id}/tickets/{ticket_id}/feed")
def get_feed(request: Request, app_id: int, ticket_id: int):
    _auth(request); _app(request, app_id)
    conn = _conn(request)
    try:
        tdx.ticket(conn, ticket_id)
        return [tdx.feed_json(r) for r in tdx.feed(conn, ticket_id)]
    finally:
        conn.close()


@router.post("/{app_id}/tickets/{ticket_id}/feed", status_code=201)
async def post_feed(request: Request, app_id: int, ticket_id: int):
    who = _auth(request); _app(request, app_id)
    body = await _json(request)
    conn = _conn(request)
    try:
        tdx.ticket(conn, ticket_id)
        return tdx.feed_json(tdx.comment(conn, _settings(request), ticket_id, who["uid"], body))
    finally:
        conn.close()


@router.get("/{app_id}/tickets/{ticket_id}/tasks")
def get_tasks(request: Request, app_id: int, ticket_id: int):
    _auth(request); _app(request, app_id)
    conn = _conn(request)
    try:
        tdx.ticket(conn, ticket_id)
        return [tdx.task_json(conn, r) for r in tdx.tasks(conn, ticket_id)]
    finally:
        conn.close()


@router.post("/{app_id}/tickets/{ticket_id}/tasks", status_code=201)
async def post_task(request: Request, app_id: int, ticket_id: int):
    who = _auth(request); _app(request, app_id)
    body = await _json(request)
    conn = _conn(request)
    try:
        return tdx.task_json(conn, tdx.create_task(conn, ticket_id, body, who["uid"]))
    finally:
        conn.close()


@router.get("/{app_id}/tickets/{ticket_id}/tasks/{task_id}")
def get_task(request: Request, app_id: int, ticket_id: int, task_id: int):
    _auth(request); _app(request, app_id)
    conn = _conn(request)
    try:
        return tdx.task_json(conn, tdx.task(conn, ticket_id, task_id))
    finally:
        conn.close()


@router.put("/{app_id}/tickets/{ticket_id}/tasks/{task_id}")
@router.patch("/{app_id}/tickets/{ticket_id}/tasks/{task_id}")
async def put_task(request: Request, app_id: int, ticket_id: int, task_id: int):
    who = _auth(request); _app(request, app_id)
    body = await _json(request)
    conn = _conn(request)
    try:
        return tdx.task_json(conn, tdx.update_task(conn, _settings(request), ticket_id, task_id, body, who["uid"]))
    finally:
        conn.close()


@router.post("/{app_id}/tickets/{ticket_id}/tasks/{task_id}/feed", status_code=201)
async def task_feed(request: Request, app_id: int, ticket_id: int, task_id: int):
    """TDX updates a task's percent complete through its feed: {PercentComplete, Comments}."""
    who = _auth(request); _app(request, app_id)
    body = await _json(request)
    conn = _conn(request)
    try:
        row = tdx.update_task(conn, _settings(request), ticket_id, task_id, {"PercentComplete": body.get("PercentComplete")}, who["uid"],
                              comment_text=body.get("Comments", ""))
        return tdx.task_json(conn, row)
    finally:
        conn.close()


@router.get("/{app_id}/tickets/{ticket_id}/workflow")
def get_workflow(request: Request, app_id: int, ticket_id: int):
    _auth(request); _app(request, app_id)
    conn = _conn(request)
    try:
        view = tdx.workflow_view(conn, ticket_id)
        return view or JSONResponse({"Message": "No workflow on this ticket"}, status_code=404)
    finally:
        conn.close()


@router.post("/{app_id}/tickets/{ticket_id}/workflow/approvals/{approval_id}")
async def workflow_approval(request: Request, app_id: int, ticket_id: int, approval_id: int):
    who = _auth(request); _app(request, app_id)
    body = await _json(request)
    conn = _conn(request)
    try:
        tdx.ticket(conn, ticket_id)
        tdx.decide_approval(conn, _settings(request), approval_id, who["uid"], bool(body.get("Approve", True)), body.get("Comments", ""))
        return tdx.workflow_view(conn, ticket_id)
    finally:
        conn.close()


# ---------------------------------------------------------------- people

@router.post("/people/lookup")
def people_lookup(request: Request, searchText: str = "", maxResults: int = 25):
    _auth(request)
    conn = _conn(request)
    try:
        return [tdx.person_json(conn, r) for r in tdx.people_lookup(conn, searchText, maxResults)]
    finally:
        conn.close()


@router.post("/people/search")
async def people_search(request: Request):
    _auth(request)
    body = await _json(request)
    conn = _conn(request)
    try:
        return [tdx.person_json(conn, r) for r in tdx.people_search(conn, body)]
    finally:
        conn.close()


@router.post("/people/import")
async def people_import(request: Request):
    """Accepts a multipart upload (field `file`) or the CSV as the raw body, whatever the Content-Type says.
    curl's --data-binary default of application/x-www-form-urlencoded must not break it."""
    _auth(request)
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        data = await request.form()
        upload = data.get("file")
        if upload is None or isinstance(upload, str):
            raise tdx.TDXError("multipart upload needs a 'file' field")
        text = (await upload.read()).decode("utf-8-sig")
    else:
        text = (await request.body()).decode("utf-8-sig")
    conn = _conn(request)
    try:
        return tdx.import_people_csv(conn, text, source="api")
    finally:
        conn.close()


@router.post("/people", status_code=201)
async def people_create(request: Request):
    _auth(request)
    body = await _json(request)
    conn = _conn(request)
    try:
        uid = tdx.upsert_person(conn, body)
        return tdx.person_json(conn, tdx.person(conn, uid))
    finally:
        conn.close()


@router.get("/people/{uid}")
def people_get(request: Request, uid: str):
    _auth(request)
    conn = _conn(request)
    try:
        row = tdx.person(conn, uid)
        if not row:
            raise tdx.NotFound("Person not found")
        return tdx.person_json(conn, row)
    finally:
        conn.close()


@router.post("/people/{uid}")
async def people_update(request: Request, uid: str):
    _auth(request)
    body = await _json(request)
    conn = _conn(request)
    try:
        if not tdx.person(conn, uid):
            raise tdx.NotFound("Person not found")
        tdx.upsert_person(conn, body, uid=uid)
        return tdx.person_json(conn, tdx.person(conn, uid))
    finally:
        conn.close()


# ---------------------------------------------------------------- audit log middleware (registered by web.py)

async def log_api_calls(request: Request, call_next):
    if not request.url.path.startswith("/TDWebApi/"):
        return await call_next(request)
    start = time.perf_counter()
    response = await call_next(request)
    caller = ""
    try:
        caller = tdx.claims(request.app.state.settings, request.headers.get("authorization")).get("name", "")
    except tdx.TDXError:
        pass
    conn = db.connect(request.app.state.settings.db_path)
    try:
        tdx.api_log(conn, request.method, request.url.path, response.status_code, caller, int((time.perf_counter() - start) * 1000))
    finally:
        conn.close()
    return response
