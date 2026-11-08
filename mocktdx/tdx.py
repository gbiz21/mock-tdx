"""Service layer: people, tickets, tasks, feed, approvals, workflow engine, automation rules, auth.

Field names and JSON shapes follow the TeamDynamix Web API so a client written against this mock
(PowerShell Invoke-RestMethod, for example) ports to the real tenant by changing the base URL and credentials.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone

from .settings import Settings
from .util import now_iso, sign_token, verify_token

log = logging.getLogger("mocktdx")

STATUS_CLASS = {"None": 0, "New": 1, "InProcess": 2, "Completed": 3, "Cancelled": 4, "OnHold": 5, "Requested": 6}
INTEGRATION_UID = str(uuid.uuid5(uuid.NAMESPACE_URL, "mock-tdx/integration"))
TICKET_FIELDS = ("Title", "Description", "AccountID", "StatusID", "PriorityID", "RequestorUid", "ResponsibleUid",
                 "ResponsibleGroupID", "StartDate", "EndDate", "TypeID", "FormID", "ServiceID")
PERSON_FIELDS = ("UserName", "FirstName", "LastName", "PrimaryEmail", "AlternateEmail", "Title", "DefaultAccountID",
                 "ReportsToUID", "IsActive", "IsEmployee", "ExternalID", "SecurityRoleName", "WorkPhone", "LocationName")


class TDXError(ValueError):
    status = 400


class NotFound(TDXError):
    status = 404


class Unauthorized(TDXError):
    status = 401


# ---------------------------------------------------------------- lookups

def _all(conn, sql, *params):
    return conn.execute(sql, params).fetchall()


def statuses(conn):      return _all(conn, "SELECT * FROM ticket_statuses WHERE IsActive = 1 ORDER BY SortOrder")
def priorities(conn):    return _all(conn, "SELECT * FROM priorities ORDER BY SortOrder")
def types(conn):         return _all(conn, "SELECT * FROM ticket_types WHERE IsActive = 1 ORDER BY ID")
def forms(conn):         return _all(conn, "SELECT * FROM forms ORDER BY ID")
def groups(conn):        return _all(conn, "SELECT * FROM groups_ WHERE IsActive = 1 ORDER BY ID")
def accounts(conn):      return _all(conn, "SELECT * FROM accounts WHERE IsActive = 1 ORDER BY ID")
def services(conn):      return _all(conn, "SELECT * FROM services WHERE IsActive = 1 ORDER BY ID")
def workflows(conn):     return _all(conn, "SELECT * FROM workflows ORDER BY ID")
def rules(conn):         return _all(conn, "SELECT * FROM automation_rules WHERE IsActive = 1 ORDER BY SortOrder, ID")


def _one(conn, table, id_):
    return conn.execute(f"SELECT * FROM {table} WHERE ID = ?", (id_,)).fetchone()


def form(conn, form_id):
    row = _one(conn, "forms", form_id)
    return dict(row, Attributes=json.loads(row["Attributes"])) if row else None


def _name(conn, table, id_, col="Name"):
    row = _one(conn, table, id_) if id_ is not None else None
    return row[col] if row else ""


# ---------------------------------------------------------------- people

def person(conn, uid):
    return conn.execute("SELECT * FROM people WHERE UID = ?", (uid,)).fetchone() if uid else None


def person_by(conn, **where):
    key, value = next(iter(where.items()))
    return conn.execute(f"SELECT * FROM people WHERE {key} = ? COLLATE NOCASE", (value,)).fetchone()


def full_name(row) -> str:
    return f"{row['FirstName']} {row['LastName']}".strip() if row else ""


def person_json(conn, row) -> dict:
    mgr = person(conn, row["ReportsToUID"])
    return {
        "UID": row["UID"], "BEID": None, "IsActive": bool(row["IsActive"]), "IsEmployee": bool(row["IsEmployee"]),
        "UserName": row["UserName"], "FullName": full_name(row), "FirstName": row["FirstName"], "LastName": row["LastName"],
        "PrimaryEmail": row["PrimaryEmail"], "AlertEmail": row["PrimaryEmail"], "AlternateEmail": row["AlternateEmail"],
        "ExternalID": row["ExternalID"], "Title": row["Title"], "WorkPhone": row["WorkPhone"],
        "DefaultAccountID": row["DefaultAccountID"], "DefaultAccountName": _name(conn, "accounts", row["DefaultAccountID"]),
        "SecurityRoleName": row["SecurityRoleName"], "LocationName": row["LocationName"],
        "ReportsToUID": row["ReportsToUID"], "ReportsToFullName": full_name(mgr),
        "GroupIDs": [g["GroupID"] for g in _all(conn, "SELECT GroupID FROM group_members WHERE UID = ?", row["UID"])],
        "CreatedDate": row["CreatedDate"], "ModifiedDate": row["ModifiedDate"],
    }


def people_lookup(conn, text: str, max_results: int = 25):
    like = f"%{text.strip()}%"
    return _all(conn, """SELECT * FROM people WHERE IsActive = 1 AND (FirstName LIKE ? OR LastName LIKE ? OR UserName LIKE ?
                         OR PrimaryEmail LIKE ? OR ExternalID LIKE ? OR (FirstName || ' ' || LastName) LIKE ?)
                         ORDER BY LastName, FirstName LIMIT ?""", like, like, like, like, like, like, max_results)


def people_search(conn, body: dict):
    where, params = [], []
    if body.get("SearchText"):
        like = f"%{body['SearchText']}%"
        where.append("(FirstName LIKE ? OR LastName LIKE ? OR UserName LIKE ? OR PrimaryEmail LIKE ? OR ExternalID LIKE ?)")
        params += [like] * 5
    if body.get("IsActive") is not None:
        where.append("IsActive = ?")
        params.append(1 if body["IsActive"] else 0)
    if body.get("ExternalID"):
        where.append("ExternalID = ?")
        params.append(body["ExternalID"])
    if body.get("AccountIDs"):
        where.append(f"DefaultAccountID IN ({','.join('?' * len(body['AccountIDs']))})")
        params += body["AccountIDs"]
    sql = "SELECT * FROM people" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY LastName, FirstName LIMIT ?"
    params.append(int(body.get("MaxResults") or 50))
    return conn.execute(sql, params).fetchall()


def upsert_person(conn, data: dict, uid: str | None = None) -> str:
    """Create or update a person. Missing UserName falls back to the email, then the ExternalID."""
    fields = {k: data[k] for k in PERSON_FIELDS if k in data and data[k] is not None}
    for key in ("IsActive", "IsEmployee"):
        if key in fields:
            fields[key] = 1 if fields[key] in (True, 1, "1", "true", "True") else 0
    now = now_iso()
    if uid and person(conn, uid):
        fields["ModifiedDate"] = now
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE people SET {sets} WHERE UID = ?", (*fields.values(), uid))
        conn.commit()
        return uid
    if not fields.get("FirstName") or not fields.get("LastName"):
        raise TDXError("FirstName and LastName are required")
    fields.setdefault("UserName", fields.get("PrimaryEmail") or fields.get("ExternalID") or str(uuid.uuid4()))
    if person_by(conn, UserName=fields["UserName"]):
        raise TDXError(f"UserName {fields['UserName']} already exists")
    uid = uid or str(uuid.uuid4())
    fields.update(UID=uid, CreatedDate=now, ModifiedDate=now)
    conn.execute(f"INSERT INTO people({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})", tuple(fields.values()))
    conn.commit()
    return uid


HR_COLUMNS = {  # HR roster header -> person field (the mock-adp roster report; adjust to the real export)
    "Associate ID": "ExternalID", "First Name": "FirstName", "Last Name": "LastName",
    "Work Contact: Work Email": "PrimaryEmail", "Job Title Description": "Title", "Location Description": "LocationName",
}


def import_people_csv(conn, text: str, source: str = "upload") -> dict:
    """TDX People Import, fed by the same HR roster file the identity pipeline uses.

    Match key is ExternalID (the HR Associate ID), so name changes update rather than duplicate.
    Terminated rows deactivate the person. Managers resolve in a second pass by Associate ID.
    """
    reader = csv.DictReader(io.StringIO(text))
    missing = [h for h in ("Associate ID", "First Name", "Last Name") if h not in (reader.fieldnames or [])]
    if missing:
        raise TDXError(f"CSV is missing required columns: {', '.join(missing)}")
    created = updated = deactivated = rows = 0
    errors: list[str] = []
    managers: list[tuple[str, str]] = []
    for rec in reader:
        rows += 1
        ext = (rec.get("Associate ID") or "").strip()
        if not ext:
            errors.append(f"row {rows}: blank Associate ID")
            continue
        data = {field: (rec.get(header) or "").strip() for header, field in HR_COLUMNS.items()}
        dept = (rec.get("Home Department Code") or "").strip()
        if dept.isdigit():
            data["DefaultAccountID"] = int(dept)
        data["IsActive"] = 0 if (rec.get("Position Status") or "").strip() == "Terminated" else 1
        data["IsEmployee"] = 1
        data["SecurityRoleName"] = "Client"
        existing = person_by(conn, ExternalID=ext)
        try:
            if existing:
                if not data["PrimaryEmail"]:
                    data.pop("PrimaryEmail")  # never blank out an email the help desk already uses
                upsert_person(conn, data, uid=existing["UID"])
                updated += 1
                if not data["IsActive"] and existing["IsActive"]:
                    deactivated += 1
            else:
                data["UserName"] = data["PrimaryEmail"] or ext
                upsert_person(conn, data, uid=str(uuid.uuid5(uuid.NAMESPACE_URL, f"mock-tdx/hr/{ext}")))
                created += 1
        except TDXError as exc:
            errors.append(f"row {rows} ({ext}): {exc}")
            continue
        mgr = (rec.get("Reports To Associate ID") or "").strip()
        if mgr:
            managers.append((ext, mgr))
    for ext, mgr_ext in managers:  # second pass: managers may appear later in the file
        p, m = person_by(conn, ExternalID=ext), person_by(conn, ExternalID=mgr_ext)
        if p and m:
            conn.execute("UPDATE people SET ReportsToUID = ? WHERE UID = ?", (m["UID"], p["UID"]))
        elif p:
            errors.append(f"{ext}: manager {mgr_ext} not found")
    conn.execute("INSERT INTO people_imports(Date, Source, Rows, Created, Updated, Deactivated, Errors) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (now_iso(), source, rows, created, updated, deactivated, "\n".join(errors)))
    conn.commit()
    return {"rows": rows, "created": created, "updated": updated, "deactivated": deactivated, "errors": errors}


def imports(conn):
    return _all(conn, "SELECT * FROM people_imports ORDER BY ID DESC LIMIT 20")


# ---------------------------------------------------------------- tickets

def ticket(conn, ticket_id: int):
    row = _one(conn, "tickets", ticket_id)
    if not row:
        raise NotFound(f"Ticket {ticket_id} not found")
    return row


def _attributes_json(conn, form_id, stored: list) -> list:
    """Merge stored {ID, Value} pairs with the form definition so the API returns Name/ValueText like TDX."""
    defs = {a["ID"]: a for a in (form(conn, form_id) or {"Attributes": []})["Attributes"]}
    out = []
    for i, item in enumerate(stored):
        d = defs.get(item["ID"], {})
        value = item.get("Value", "")
        choices = {str(c.get("ID", c.get("Name"))): c.get("Name") for c in d.get("Choices", [])}
        out.append({"ID": item["ID"], "Name": d.get("Name", f"Attribute {item['ID']}"), "Order": i,
                    "FieldType": d.get("FieldType", "text"), "DataType": d.get("DataType", "String"),
                    "Value": value, "ValueText": choices.get(str(value), str(value))})
    return out


def ticket_json(conn, row) -> dict:
    req, resp, cre, mod = person(conn, row["RequestorUid"]), person(conn, row["ResponsibleUid"]), person(conn, row["CreatedUid"]), person(conn, row["ModifiedUid"])
    st = _one(conn, "ticket_statuses", row["StatusID"])
    inst = conn.execute("SELECT * FROM workflow_instances WHERE ID = ?", (row["WorkflowInstanceID"],)).fetchone() if row["WorkflowInstanceID"] else None
    wf = _one(conn, "workflows", inst["WorkflowID"]) if inst else None
    svc = _one(conn, "services", row["ServiceID"]) if row["ServiceID"] else None
    return {
        "ID": row["ID"], "ParentID": 0, "AppID": row["AppID"], "TypeID": row["TypeID"], "TypeName": _name(conn, "ticket_types", row["TypeID"]),
        "TypeCategoryName": _name(conn, "ticket_types", row["TypeID"], "CategoryName"), "Classification": "Service Request",
        "FormID": row["FormID"], "FormName": _name(conn, "forms", row["FormID"]), "Title": row["Title"], "Description": row["Description"],
        "Uri": f"/TDNext/Apps/{row['AppID']}/Tickets/{row['ID']}", "AccountID": row["AccountID"], "AccountName": _name(conn, "accounts", row["AccountID"]),
        "SourceName": row["SourceName"], "StatusID": row["StatusID"], "StatusName": st["Name"] if st else "", "StatusClass": STATUS_CLASS.get(st["StatusClass"] if st else "None", 0),
        "PriorityID": row["PriorityID"], "PriorityName": _name(conn, "priorities", row["PriorityID"]),
        "RequestorUid": row["RequestorUid"], "RequestorName": full_name(req), "RequestorFirstName": req["FirstName"] if req else "",
        "RequestorLastName": req["LastName"] if req else "", "RequestorEmail": req["PrimaryEmail"] if req else "",
        "ResponsibleUid": row["ResponsibleUid"], "ResponsibleFullName": full_name(resp), "ResponsibleEmail": resp["PrimaryEmail"] if resp else "",
        "ResponsibleGroupID": row["ResponsibleGroupID"], "ResponsibleGroupName": _name(conn, "groups_", row["ResponsibleGroupID"]),
        "CreatedUid": row["CreatedUid"], "CreatedFullName": full_name(cre), "CreatedDate": row["CreatedDate"],
        "ModifiedUid": row["ModifiedUid"], "ModifiedFullName": full_name(mod), "ModifiedDate": row["ModifiedDate"],
        "StartDate": row["StartDate"], "EndDate": row["EndDate"], "CompletedDate": row["CompletedDate"],
        "ServiceID": row["ServiceID"], "ServiceName": svc["Name"] if svc else "",
        "WorkflowID": wf["ID"] if wf else None, "WorkflowName": wf["Name"] if wf else "", "WorkflowStatus": inst["Status"] if inst else "",
        "Attributes": _attributes_json(conn, row["FormID"], json.loads(row["Attributes"])),
    }


def _normalize_attributes(conn, form_id, attrs) -> list:
    out = []
    defs = {a["ID"]: a for a in (form(conn, form_id) or {"Attributes": []})["Attributes"]} if form_id else {}
    for item in attrs or []:
        try:
            aid = int(item["ID"])
        except (KeyError, TypeError, ValueError):
            raise TDXError("Each attribute needs an integer ID")
        value = item.get("Value", "")
        d = defs.get(aid)
        if d and d.get("Choices"):
            valid = {str(c.get("ID", c.get("Name"))) for c in d["Choices"]} | {c["Name"] for c in d["Choices"]}
            if str(value) and str(value) not in valid:
                raise TDXError(f"Attribute '{d['Name']}' must be one of {sorted(c['Name'] for c in d['Choices'])}")
        out.append({"ID": aid, "Value": value})
    return out


def create_ticket(conn, s: Settings, data: dict, by_uid: str, source: str = "Web Service"):
    if not (data.get("Title") or "").strip():
        raise TDXError("Title is required")
    type_id = int(data.get("TypeID") or 0)
    if not _one(conn, "ticket_types", type_id):
        raise TDXError(f"Unknown TypeID {type_id}")
    status_id = int(data.get("StatusID") or 77)
    if not _one(conn, "ticket_statuses", status_id):
        raise TDXError(f"Unknown StatusID {status_id}")
    priority_id = int(data.get("PriorityID") or 2)
    if not _one(conn, "priorities", priority_id):
        raise TDXError(f"Unknown PriorityID {priority_id}")
    requestor = person(conn, data.get("RequestorUid")) if data.get("RequestorUid") else None
    if not requestor and data.get("RequestorEmail"):
        requestor = person_by(conn, PrimaryEmail=data["RequestorEmail"])
    if data.get("RequestorUid") and not requestor:
        raise TDXError("RequestorUid does not match a person")
    form_id = data.get("FormID")
    if form_id and not _one(conn, "forms", int(form_id)):
        raise TDXError(f"Unknown FormID {form_id}")
    group_id = data.get("ResponsibleGroupID")
    if group_id and not _one(conn, "groups_", int(group_id)):
        raise TDXError(f"Unknown ResponsibleGroupID {group_id}")
    account_id = data.get("AccountID") or (requestor["DefaultAccountID"] if requestor else None)
    now = now_iso()
    cur = conn.execute(
        """INSERT INTO tickets(AppID, TypeID, FormID, ServiceID, Title, Description, AccountID, StatusID, PriorityID, RequestorUid,
               ResponsibleUid, ResponsibleGroupID, CreatedUid, CreatedDate, ModifiedUid, ModifiedDate, StartDate, EndDate, Attributes, SourceName)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (s.app_id, type_id, int(form_id) if form_id else None, data.get("ServiceID"), data["Title"].strip(), data.get("Description") or "",
         account_id, status_id, priority_id, requestor["UID"] if requestor else None, data.get("ResponsibleUid"),
         int(group_id) if group_id else None, by_uid, now, by_uid, now, data.get("StartDate"), data.get("EndDate"),
         json.dumps(_normalize_attributes(conn, form_id, data.get("Attributes"))), source))
    ticket_id = cur.lastrowid
    add_feed(conn, ticket_id, by_uid, f"Ticket created via {source}.", entry_type="System")
    conn.commit()
    apply_rules(conn, s, ticket_id, by_uid)
    log.info("ticket created", extra={"extra": {"ticket": ticket_id, "type": type_id, "source": source, "by": by_uid}})
    return ticket(conn, ticket_id)


def update_ticket_fields(conn, s: Settings, ticket_id: int, fields: dict, by_uid: str):
    row = ticket(conn, ticket_id)
    changes = {k: v for k, v in fields.items() if k in TICKET_FIELDS and v is not None and v != row[k]}
    if "Attributes" in fields:
        changes["Attributes"] = json.dumps(_normalize_attributes(conn, fields.get("FormID", row["FormID"]), fields["Attributes"]))
    if not changes:
        return row
    if "StatusID" in changes:
        new = _one(conn, "ticket_statuses", int(changes["StatusID"]))
        if not new:
            raise TDXError(f"Unknown StatusID {changes['StatusID']}")
        old = _one(conn, "ticket_statuses", row["StatusID"])
        changes["CompletedDate"] = now_iso() if new["StatusClass"] in ("Completed", "Cancelled") else None
        add_feed(conn, ticket_id, by_uid, "", old_status=old["Name"] if old else "", new_status=new["Name"], entry_type="Status")
    if "ResponsibleGroupID" in changes and changes["ResponsibleGroupID"] and not _one(conn, "groups_", int(changes["ResponsibleGroupID"])):
        raise TDXError("Unknown ResponsibleGroupID")
    if "ResponsibleUid" in changes and changes["ResponsibleUid"] and not person(conn, changes["ResponsibleUid"]):
        raise TDXError("Unknown ResponsibleUid")
    changes.update(ModifiedUid=by_uid, ModifiedDate=now_iso())
    sets = ", ".join(f"{k} = ?" for k in changes)
    conn.execute(f"UPDATE tickets SET {sets} WHERE ID = ?", (*changes.values(), ticket_id))
    conn.commit()
    return ticket(conn, ticket_id)


def patch_ticket(conn, s: Settings, ticket_id: int, ops: list, by_uid: str):
    """JSON Patch subset: replace/add on top-level ticket fields, e.g. [{"op":"replace","path":"/StatusID","value":81}]."""
    fields = {}
    for op in ops or []:
        if op.get("op") not in ("replace", "add"):
            raise TDXError(f"Unsupported op {op.get('op')!r}")
        key = (op.get("path") or "").lstrip("/")
        if key not in TICKET_FIELDS and key != "Attributes":
            raise TDXError(f"Field {key!r} is not patchable")
        fields[key] = op.get("value")
    return update_ticket_fields(conn, s, ticket_id, fields, by_uid)


def search_tickets(conn, s: Settings, body: dict):
    where, params = ["AppID = ?"], [s.app_id]
    if body.get("SearchText"):
        like = f"%{body['SearchText']}%"
        where.append("(Title LIKE ? OR Description LIKE ? OR CAST(ID AS TEXT) = ?)")
        params += [like, like, body["SearchText"]]
    for key, col in (("StatusIDs", "StatusID"), ("TypeIDs", "TypeID"), ("PriorityIDs", "PriorityID"),
                     ("ResponsibilityGroupIDs", "ResponsibleGroupID"), ("ResponsibilityUids", "ResponsibleUid"),
                     ("RequestorUids", "RequestorUid"), ("FormIDs", "FormID")):
        if body.get(key):
            where.append(f"{col} IN ({','.join('?' * len(body[key]))})")
            params += list(body[key])
    if body.get("StatusClassIDs"):
        names = [n for n, v in STATUS_CLASS.items() if v in body["StatusClassIDs"]]
        where.append(f"StatusID IN (SELECT ID FROM ticket_statuses WHERE StatusClass IN ({','.join('?' * len(names))}))")
        params += names
    if body.get("CreatedDateFrom"):
        where.append("CreatedDate >= ?")
        params.append(body["CreatedDateFrom"])
    if body.get("CreatedDateTo"):
        where.append("CreatedDate <= ?")
        params.append(body["CreatedDateTo"])
    rows = conn.execute("SELECT * FROM tickets WHERE " + " AND ".join(where) + " ORDER BY ID DESC", params).fetchall()
    if body.get("CustomAttributes"):
        wanted = {int(a["ID"]): str(a.get("Value", "")) for a in body["CustomAttributes"]}
        rows = [r for r in rows if all(any(a["ID"] == k and str(a.get("Value", "")) == v for a in json.loads(r["Attributes"]))
                                       for k, v in wanted.items())]
    return rows[: int(body.get("MaxResults") or 50)]


def list_tickets(conn, s: Settings, status_class: str | None = None, group_id: int | None = None, q: str | None = None,
                 requestor_uid: str | None = None):
    body: dict = {"MaxResults": 500}
    if q:
        body["SearchText"] = q
    if status_class == "open":
        body["StatusClassIDs"] = [1, 2, 5]
    elif status_class == "closed":
        body["StatusClassIDs"] = [3, 4]
    if group_id:
        body["ResponsibilityGroupIDs"] = [group_id]
    if requestor_uid:
        body["RequestorUids"] = [requestor_uid]
    return search_tickets(conn, s, body)


# ---------------------------------------------------------------- feed

def add_feed(conn, ticket_id: int, by_uid: str | None, comments: str, *, new_status_id: int | None = None,
             old_status: str | None = None, new_status: str | None = None, is_private: bool = False,
             entry_type: str = "Comment", task_id: int | None = None):
    who = person(conn, by_uid)
    conn.execute(
        "INSERT INTO ticket_feed(TicketID, TaskID, CreatedUid, CreatedFullName, CreatedDate, Body, IsPrivate, OldStatus, NewStatus, EntryType) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, task_id, by_uid, full_name(who) if who else ("HR Provisioning (integration)" if by_uid == INTEGRATION_UID else "System"),
         now_iso(), comments, int(is_private), old_status, new_status, entry_type))
    conn.commit()


def feed(conn, ticket_id: int):
    return _all(conn, "SELECT * FROM ticket_feed WHERE TicketID = ? ORDER BY ID DESC", ticket_id)


def feed_json(row) -> dict:
    return {"ID": row["ID"], "TicketID": row["TicketID"], "TaskID": row["TaskID"], "CreatedUid": row["CreatedUid"],
            "CreatedFullName": row["CreatedFullName"], "CreatedDate": row["CreatedDate"], "Body": row["Body"],
            "IsPrivate": bool(row["IsPrivate"]), "OldStatus": row["OldStatus"], "NewStatus": row["NewStatus"], "EntryType": row["EntryType"]}


def comment(conn, s: Settings, ticket_id: int, by_uid: str, body: dict):
    """POST /tickets/{id}/feed: {Comments, NewStatusID, IsPrivate, Notify}."""
    text = (body.get("Comments") or "").strip()
    if body.get("NewStatusID"):
        update_ticket_fields(conn, s, ticket_id, {"StatusID": int(body["NewStatusID"])}, by_uid)
    if text:
        add_feed(conn, ticket_id, by_uid, text, is_private=bool(body.get("IsPrivate")))
    return feed(conn, ticket_id)[0]


# ---------------------------------------------------------------- tasks

def tasks(conn, ticket_id: int):
    return _all(conn, "SELECT * FROM ticket_tasks WHERE TicketID = ? ORDER BY SortOrder, ID", ticket_id)


def task(conn, ticket_id: int, task_id: int):
    row = conn.execute("SELECT * FROM ticket_tasks WHERE TicketID = ? AND ID = ?", (ticket_id, task_id)).fetchone()
    if not row:
        raise NotFound(f"Task {task_id} not found on ticket {ticket_id}")
    return row


def task_json(conn, row) -> dict:
    resp = person(conn, row["ResponsibleUid"])
    done = person(conn, row["CompletedUid"])
    return {"ID": row["ID"], "TicketID": row["TicketID"], "Title": row["Title"], "Description": row["Description"],
            "ResponsibleGroupID": row["ResponsibleGroupID"], "ResponsibleGroupName": _name(conn, "groups_", row["ResponsibleGroupID"]),
            "ResponsibleUid": row["ResponsibleUid"], "ResponsibleFullName": full_name(resp),
            "StartDate": row["StartDate"], "EndDate": row["EndDate"], "PercentComplete": row["PercentComplete"],
            "CompletedDate": row["CompletedDate"], "CompletedUid": row["CompletedUid"], "CompletedFullName": full_name(done),
            "Order": row["SortOrder"], "WorkflowStepID": row["WorkflowStepID"], "CreatedDate": row["CreatedDate"], "ModifiedDate": row["ModifiedDate"]}


def create_task(conn, ticket_id: int, data: dict, by_uid: str, step_id: str | None = None):
    ticket(conn, ticket_id)
    if not (data.get("Title") or "").strip():
        raise TDXError("Task Title is required")
    group_id = data.get("ResponsibleGroupID")
    if group_id and not _one(conn, "groups_", int(group_id)):
        raise TDXError("Unknown ResponsibleGroupID")
    order = conn.execute("SELECT COALESCE(MAX(SortOrder), 0) + 1 FROM ticket_tasks WHERE TicketID = ?", (ticket_id,)).fetchone()[0]
    now = now_iso()
    cur = conn.execute(
        "INSERT INTO ticket_tasks(TicketID, Title, Description, ResponsibleGroupID, ResponsibleUid, StartDate, EndDate, SortOrder, WorkflowStepID, CreatedDate, ModifiedDate) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, data["Title"].strip(), data.get("Description") or "", int(group_id) if group_id else None, data.get("ResponsibleUid"),
         data.get("StartDate"), data.get("EndDate"), order, step_id, now, now))
    add_feed(conn, ticket_id, by_uid, f"Task added: {data['Title'].strip()}", entry_type="Task", task_id=cur.lastrowid)
    conn.commit()
    return task(conn, ticket_id, cur.lastrowid)


def update_task(conn, s: Settings, ticket_id: int, task_id: int, changes: dict, by_uid: str, comment_text: str = ""):
    row = task(conn, ticket_id, task_id)
    fields = {k: changes[k] for k in ("Title", "Description", "ResponsibleGroupID", "ResponsibleUid", "StartDate", "EndDate", "PercentComplete") if k in changes and changes[k] is not None}
    completed_now = False
    if "PercentComplete" in fields:
        pct = max(0, min(100, int(fields["PercentComplete"])))
        fields["PercentComplete"] = pct
        if pct == 100 and row["PercentComplete"] < 100:
            fields.update(CompletedDate=now_iso(), CompletedUid=by_uid)
            completed_now = True
    if fields:
        fields["ModifiedDate"] = now_iso()
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE ticket_tasks SET {sets} WHERE ID = ?", (*fields.values(), task_id))
    if comment_text or completed_now:
        add_feed(conn, ticket_id, by_uid, comment_text or f"Task completed: {row['Title']}", entry_type="Task", task_id=task_id)
    conn.commit()
    if completed_now:
        inst = conn.execute("SELECT WorkflowInstanceID FROM tickets WHERE ID = ?", (ticket_id,)).fetchone()["WorkflowInstanceID"]
        if inst:
            advance(conn, s, inst, by_uid)
    return task(conn, ticket_id, task_id)


# ---------------------------------------------------------------- approvals

def approvals(conn, ticket_id: int):
    return _all(conn, "SELECT * FROM approvals WHERE TicketID = ? ORDER BY ID", ticket_id)


def decide_approval(conn, s: Settings, approval_id: int, by_uid: str, approved: bool, comment_text: str = ""):
    row = _one(conn, "approvals", approval_id)
    if not row:
        raise NotFound("Approval not found")
    if row["Status"] != "Pending":
        raise TDXError("Approval already decided")
    status = "Approved" if approved else "Rejected"
    conn.execute("UPDATE approvals SET Status = ?, DecidedDate = ?, DecidedUid = ?, Comment = ? WHERE ID = ?",
                 (status, now_iso(), by_uid, comment_text, approval_id))
    add_feed(conn, row["TicketID"], by_uid, f"Approval {status.lower()}: {row['Title']}. {comment_text}".strip(), entry_type="Workflow")
    conn.commit()
    advance(conn, s, row["InstanceID"], by_uid)


# ---------------------------------------------------------------- workflow engine

def instance(conn, ticket_id: int):
    row = ticket(conn, ticket_id)
    return conn.execute("SELECT * FROM workflow_instances WHERE ID = ?", (row["WorkflowInstanceID"],)).fetchone() if row["WorkflowInstanceID"] else None


def _definition(conn, workflow_id: int) -> dict:
    row = _one(conn, "workflows", workflow_id)
    if not row:
        raise TDXError(f"Unknown workflow {workflow_id}")
    return json.loads(row["Definition"])


def start_workflow(conn, s: Settings, ticket_id: int, workflow_id: int, by_uid: str):
    row = ticket(conn, ticket_id)
    if row["WorkflowInstanceID"]:
        raise TDXError("Ticket already has a workflow")
    wf = _one(conn, "workflows", workflow_id)
    if not wf:
        raise TDXError(f"Unknown workflow {workflow_id}")
    cur = conn.execute("INSERT INTO workflow_instances(TicketID, WorkflowID, StartedDate) VALUES (?, ?, ?)", (ticket_id, workflow_id, now_iso()))
    conn.execute("UPDATE tickets SET WorkflowInstanceID = ?, ModifiedDate = ? WHERE ID = ?", (cur.lastrowid, now_iso(), ticket_id))
    add_feed(conn, ticket_id, by_uid, f"Workflow started: {wf['Name']}", entry_type="Workflow")
    conn.commit()
    advance(conn, s, cur.lastrowid, by_uid)


def _stage_blocked(conn, inst, stage: dict) -> bool:
    step_ids = [st["id"] for st in stage["steps"]]
    if not step_ids:
        return False
    q = ",".join("?" * len(step_ids))
    open_tasks = conn.execute(f"SELECT COUNT(*) FROM ticket_tasks WHERE TicketID = ? AND WorkflowStepID IN ({q}) AND PercentComplete < 100",
                              (inst["TicketID"], *step_ids)).fetchone()[0]
    pending = conn.execute(f"SELECT COUNT(*) FROM approvals WHERE InstanceID = ? AND StepID IN ({q}) AND Status = 'Pending'",
                           (inst["ID"], *step_ids)).fetchone()[0]
    return bool(open_tasks or pending)


def _run_stage(conn, s: Settings, inst, stage: dict, by_uid: str):
    t = ticket(conn, inst["TicketID"])
    today = date.today()
    for step in stage["steps"]:
        kind = step["type"]
        if kind == "task":
            due = (today + timedelta(days=int(step.get("dueDays", 1)))).isoformat()
            create_task(conn, t["ID"], {"Title": step["title"], "Description": step.get("description", ""),
                                        "ResponsibleGroupID": step.get("group"), "StartDate": today.isoformat(), "EndDate": due}, by_uid, step_id=step["id"])
        elif kind == "approval":
            approver = t["RequestorUid"]
            if step.get("approver") == "requestor_manager":
                req = person(conn, t["RequestorUid"])
                approver = req["ReportsToUID"] if req and req["ReportsToUID"] else t["RequestorUid"]
            conn.execute("INSERT INTO approvals(TicketID, InstanceID, StepID, Title, ApproverUid, CreatedDate) VALUES (?, ?, ?, ?, ?, ?)",
                         (t["ID"], inst["ID"], step["id"], step["title"], approver, now_iso()))
            add_feed(conn, t["ID"], by_uid, f"Approval requested from {full_name(person(conn, approver)) or 'requestor'}: {step['title']}", entry_type="Workflow")
        elif kind == "notify":
            req = person(conn, t["RequestorUid"])
            add_feed(conn, t["ID"], by_uid, f"Notification sent to {req['PrimaryEmail'] if req else 'requestor'}: {step.get('subject', '')} — {step.get('body', '')}", entry_type="Notification")
        elif kind == "webhook":
            result = _webhook(s, {"event": step.get("event", "ticket.event"), "ticket": ticket_json(conn, t)})
            add_feed(conn, t["ID"], by_uid, f"Web service step '{step.get('event')}': {result}", entry_type="Workflow")
        elif kind == "status":
            update_ticket_fields(conn, s, t["ID"], {"StatusID": int(step["status"])}, by_uid)
    conn.commit()


def _webhook(s: Settings, payload: dict) -> str:
    if not s.webhook_url:
        return "no WEBHOOK_URL configured, logged only"
    try:
        req = urllib.request.Request(s.webhook_url, data=json.dumps(payload, default=str).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return f"POST {s.webhook_url} -> {resp.status}"
    except Exception as exc:  # the workflow must not die because a webhook did
        return f"POST {s.webhook_url} failed: {exc}"


def advance(conn, s: Settings, instance_id: int, by_uid: str = INTEGRATION_UID):
    """Move the instance forward until a stage blocks (open task or pending approval) or the workflow ends."""
    while True:
        inst = conn.execute("SELECT * FROM workflow_instances WHERE ID = ?", (instance_id,)).fetchone()
        if not inst or inst["Status"] != "InProgress":
            return
        stages = _definition(conn, inst["WorkflowID"])["stages"]
        if inst["CurrentStage"] >= 0:
            stage = stages[inst["CurrentStage"]]
            rejected = conn.execute("SELECT COUNT(*) FROM approvals WHERE InstanceID = ? AND Status = 'Rejected'", (instance_id,)).fetchone()[0]
            if rejected:
                conn.execute("UPDATE workflow_instances SET Status = 'Cancelled', CompletedDate = ? WHERE ID = ?", (now_iso(), instance_id))
                update_ticket_fields(conn, s, inst["TicketID"], {"StatusID": 80}, by_uid)  # On Hold
                add_feed(conn, inst["TicketID"], by_uid, "Workflow cancelled: an approval was rejected. Ticket placed On Hold.", entry_type="Workflow")
                conn.commit()
                return
            if _stage_blocked(conn, inst, stage):
                return
            add_feed(conn, inst["TicketID"], by_uid, f"Workflow stage complete: {stage['name']}", entry_type="Workflow")
        nxt = inst["CurrentStage"] + 1
        if nxt >= len(stages):
            conn.execute("UPDATE workflow_instances SET Status = 'Completed', CompletedDate = ? WHERE ID = ?", (now_iso(), instance_id))
            add_feed(conn, inst["TicketID"], by_uid, "Workflow completed.", entry_type="Workflow")
            conn.commit()
            return
        conn.execute("UPDATE workflow_instances SET CurrentStage = ? WHERE ID = ?", (nxt, instance_id))
        conn.commit()
        inst = conn.execute("SELECT * FROM workflow_instances WHERE ID = ?", (instance_id,)).fetchone()
        add_feed(conn, inst["TicketID"], by_uid, f"Workflow stage started: {stages[nxt]['name']}", entry_type="Workflow")
        _run_stage(conn, s, inst, stages[nxt], by_uid)


def workflow_view(conn, ticket_id: int) -> dict | None:
    inst = instance(conn, ticket_id)
    if not inst:
        return None
    wf = _one(conn, "workflows", inst["WorkflowID"])
    stages = json.loads(wf["Definition"])["stages"]
    task_by_step = {t["WorkflowStepID"]: t for t in tasks(conn, ticket_id) if t["WorkflowStepID"]}
    appr_by_step = {a["StepID"]: a for a in approvals(conn, ticket_id)}
    view = []
    for i, stage in enumerate(stages):
        steps = []
        for st in stage["steps"]:
            state = "pending"
            if st["type"] == "task" and st["id"] in task_by_step:
                state = "done" if task_by_step[st["id"]]["PercentComplete"] >= 100 else "open"
            elif st["type"] == "approval" and st["id"] in appr_by_step:
                state = appr_by_step[st["id"]]["Status"].lower()
            elif i < inst["CurrentStage"] or (i == inst["CurrentStage"] and inst["Status"] == "Completed"):
                state = "done"
            elif i == inst["CurrentStage"]:
                state = "done" if st["type"] in ("notify", "webhook", "status") else "open"
            steps.append({**st, "state": state})
        view.append({"name": stage["name"], "index": i, "current": i == inst["CurrentStage"] and inst["Status"] == "InProgress",
                     "done": i < inst["CurrentStage"] or inst["Status"] == "Completed", "steps": steps})
    return {"ID": inst["ID"], "WorkflowID": wf["ID"], "Name": wf["Name"], "Status": inst["Status"], "CurrentStage": inst["CurrentStage"],
            "StartedDate": inst["StartedDate"], "CompletedDate": inst["CompletedDate"], "Stages": view}


# ---------------------------------------------------------------- automation rules

def apply_rules(conn, s: Settings, ticket_id: int, by_uid: str):
    t = ticket(conn, ticket_id)
    for rule in rules(conn):
        if rule["Trigger"] != "TicketCreated":
            continue
        cond = json.loads(rule["Conditions"])
        if any(t[k] != v for k, v in cond.items()):
            continue
        actions = json.loads(rule["Actions"])
        fields = {k: v for k, v in actions.items() if k in ("StatusID", "PriorityID", "ResponsibleGroupID", "ResponsibleUid")}
        if fields:
            update_ticket_fields(conn, s, ticket_id, fields, by_uid)
        if actions.get("Comment"):
            add_feed(conn, ticket_id, by_uid, actions["Comment"], entry_type="System")
        add_feed(conn, ticket_id, by_uid, f"Automation rule applied: {rule['Name']}", entry_type="System")
        if actions.get("StartWorkflowID") and not ticket(conn, ticket_id)["WorkflowInstanceID"]:
            start_workflow(conn, s, ticket_id, int(actions["StartWorkflowID"]), by_uid)


# ---------------------------------------------------------------- auth

def login_admin(s: Settings, beid: str, key: str) -> str | None:
    if not s.web_services_key or beid != s.beid or key != s.web_services_key:
        return None
    return sign_token(s.token_secret, {"uid": INTEGRATION_UID, "name": "HR Provisioning (integration)", "kind": "admin"}, s.token_ttl_hours * 3600)


def login_user(conn, s: Settings, username: str, password: str) -> str | None:
    row = person_by(conn, UserName=username)
    if not row or not row["IsActive"] or row["SecurityRoleName"] == "Client" or not s.tech_password or password != s.tech_password:
        return None
    return sign_token(s.token_secret, {"uid": row["UID"], "name": full_name(row), "kind": "user"}, s.token_ttl_hours * 3600)


def claims(s: Settings, authorization: str | None) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("Missing bearer token")
    body = verify_token(s.token_secret, authorization.split(" ", 1)[1].strip())
    if not body:
        raise Unauthorized("Invalid or expired token")
    return body


def api_log(conn, method: str, path: str, status: int, caller: str, ms: int) -> None:
    conn.execute("INSERT INTO api_log(Date, Method, Path, Status, Caller, DurationMs) VALUES (?, ?, ?, ?, ?, ?)",
                 (now_iso(), method, path, status, caller, ms))
    conn.commit()


def api_logs(conn, limit: int = 100):
    return _all(conn, "SELECT * FROM api_log ORDER BY ID DESC LIMIT ?", limit)


# ---------------------------------------------------------------- desktop helpers

def counts(conn, s: Settings) -> dict:
    one = lambda sql, *p: conn.execute(sql, p).fetchone()[0]
    return {
        "new": one("SELECT COUNT(*) FROM tickets WHERE AppID = ? AND StatusID IN (SELECT ID FROM ticket_statuses WHERE StatusClass = 'New')", s.app_id),
        "in_process": one("SELECT COUNT(*) FROM tickets WHERE AppID = ? AND StatusID IN (SELECT ID FROM ticket_statuses WHERE StatusClass IN ('InProcess','OnHold'))", s.app_id),
        "open_tasks": one("SELECT COUNT(*) FROM ticket_tasks WHERE PercentComplete < 100"),
        "overdue_tasks": one("SELECT COUNT(*) FROM ticket_tasks WHERE PercentComplete < 100 AND EndDate < ?", date.today().isoformat()),
        "pending_approvals": one("SELECT COUNT(*) FROM approvals WHERE Status = 'Pending'"),
        "resolved_7d": one("SELECT COUNT(*) FROM tickets WHERE AppID = ? AND CompletedDate >= ?", s.app_id, (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")),
        "people": one("SELECT COUNT(*) FROM people WHERE IsActive = 1"),
    }


def my_work(conn, uid: str):
    gids = [g["GroupID"] for g in _all(conn, "SELECT GroupID FROM group_members WHERE UID = ?", uid)]
    q = ",".join("?" * len(gids)) or "NULL"
    open_tasks = _all(conn, f"""SELECT tk.*, t.Title AS TicketTitle FROM ticket_tasks tk JOIN tickets t ON t.ID = tk.TicketID
                               WHERE tk.PercentComplete < 100 AND (tk.ResponsibleUid = ? OR tk.ResponsibleGroupID IN ({q}))
                               ORDER BY tk.EndDate, tk.ID""", uid, *gids)
    my_tickets = _all(conn, f"""SELECT * FROM tickets WHERE StatusID IN (SELECT ID FROM ticket_statuses WHERE StatusClass IN ('New','InProcess','OnHold'))
                               AND (ResponsibleUid = ? OR ResponsibleGroupID IN ({q})) ORDER BY ID DESC""", uid, *gids)
    pending = _all(conn, "SELECT a.*, t.Title AS TicketTitle FROM approvals a JOIN tickets t ON t.ID = a.TicketID WHERE a.Status = 'Pending' AND a.ApproverUid = ? ORDER BY a.ID", uid)
    return {"tasks": open_tasks, "tickets": my_tickets, "approvals": pending}
