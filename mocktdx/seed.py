"""Reference data for a small bank's IT ticketing app, plus the onboarding/offboarding workflows."""
from __future__ import annotations

import json
import sqlite3
import uuid

from . import tdx
from .settings import Settings
from .util import now_iso

DEPARTMENTS = [(100, "Executive", "000100"), (200, "Retail Banking", "000200"), (300, "Lending", "000300"),
               (400, "Operations", "000400"), (500, "Finance & Accounting", "000500"), (600, "Compliance & Risk", "000600"),
               (700, "Information Technology", "000700"), (800, "Human Resources", "000800"), (900, "Marketing", "000900")]
GROUPS = [(101, "IT Help Desk", "Tier 1 intake and triage"), (102, "Identity & Access", "Accounts, groups, licences, MFA"),
          (103, "Desktop Support", "Devices, Autopilot, peripherals"), (104, "Facilities", "Badges, keys, workspace"),
          (105, "Human Resources", "HR partners"), (106, "Information Security", "Access reviews, incidents")]
STATUSES = [(77, "New", "New", 1), (78, "Open", "InProcess", 2), (79, "In Process", "InProcess", 3), (80, "On Hold", "OnHold", 4),
            (81, "Resolved", "Completed", 5), (82, "Closed", "Completed", 6), (83, "Cancelled", "Cancelled", 7)]
PRIORITIES = [(1, "Low", 1), (2, "Medium", 2), (3, "High", 3), (4, "Emergency", 4)]
TYPES = [(2001, "Employee Onboarding", "Human Resources"), (2002, "Employee Offboarding", "Human Resources"),
         (2003, "Access Request", "Accounts & Access"), (2004, "Hardware Request", "Equipment"), (2005, "Incident", "Service Desk")]
LOCATIONS = ["Main Branch", "Harbor Branch", "Operations Center"]


def _choices(*names):
    return [{"ID": i + 1, "Name": n} for i, n in enumerate(names)]


FORMS = [
    (3001, "Employee Onboarding", 2001, [
        {"ID": 1001, "Name": "Associate ID", "FieldType": "text", "DataType": "String", "IsRequired": True},
        {"ID": 1002, "Name": "Start Date", "FieldType": "date", "DataType": "DateTime", "IsRequired": True},
        {"ID": 1003, "Name": "Job Title", "FieldType": "text", "DataType": "String"},
        {"ID": 1004, "Name": "Home Department", "FieldType": "text", "DataType": "String"},
        {"ID": 1005, "Name": "Location", "FieldType": "dropdown", "DataType": "String", "Choices": _choices(*LOCATIONS)},
        {"ID": 1006, "Name": "Manager", "FieldType": "text", "DataType": "String"},
        {"ID": 1007, "Name": "Worker Category", "FieldType": "dropdown", "DataType": "String", "Choices": _choices("Full Time", "Part Time", "Temporary")},
        {"ID": 1008, "Name": "Hardware Profile", "FieldType": "dropdown", "DataType": "String", "Choices": _choices("Standard laptop", "Power laptop", "Desktop", "None")},
        {"ID": 1009, "Name": "Work Email", "FieldType": "text", "DataType": "String"},
    ]),
    (3002, "Employee Offboarding", 2002, [
        {"ID": 1001, "Name": "Associate ID", "FieldType": "text", "DataType": "String", "IsRequired": True},
        {"ID": 1010, "Name": "Termination Date", "FieldType": "date", "DataType": "DateTime", "IsRequired": True},
        {"ID": 1011, "Name": "Last Day Worked", "FieldType": "date", "DataType": "DateTime"},
        {"ID": 1006, "Name": "Manager", "FieldType": "text", "DataType": "String"},
        {"ID": 1012, "Name": "Mailbox Action", "FieldType": "dropdown", "DataType": "String", "Choices": _choices("Convert to shared", "Forward to manager", "None")},
        {"ID": 1013, "Name": "Equipment Return", "FieldType": "dropdown", "DataType": "String", "Choices": _choices("Branch drop-off", "Courier", "N/A")},
    ]),
    (3003, "Access Request", 2003, [
        {"ID": 1014, "Name": "System", "FieldType": "dropdown", "DataType": "String", "Choices": _choices("Core banking", "Loan origination", "Shared drive", "Reporting", "Other")},
        {"ID": 1015, "Name": "Access Level", "FieldType": "text", "DataType": "String"},
        {"ID": 1016, "Name": "Business Justification", "FieldType": "textarea", "DataType": "String", "IsRequired": True},
    ]),
    (3004, "Hardware Request", 2004, [
        {"ID": 1008, "Name": "Hardware Profile", "FieldType": "dropdown", "DataType": "String", "Choices": _choices("Standard laptop", "Power laptop", "Desktop", "Monitor", "Headset")},
        {"ID": 1017, "Name": "Needed By", "FieldType": "date", "DataType": "DateTime"},
    ]),
]

SERVICES = [
    (7001, "New Employee Onboarding", "Accounts, device, badge and day-one readiness for a new hire. Normally opened automatically from the HR feed; use this form for exceptions such as contractors.", "Human Resources", 2001, 3001),
    (7002, "Employee Separation (Offboarding)", "Access removal and equipment return for a leaver. Normally opened automatically from the HR feed.", "Human Resources", 2002, 3002),
    (7003, "Request System Access", "Access to a system or share beyond the role baseline. Requires manager approval.", "Accounts & Access", 2003, 3003),
    (7004, "Request Hardware", "A laptop, monitor or peripheral.", "Equipment", 2004, 3004),
]

WORKFLOWS = [
    (5001, "Employee Onboarding", "Identity first, then equipment and access in parallel with manager confirmation, then day-one readiness.", {"stages": [
        {"name": "Identity", "steps": [
            {"id": "s1", "type": "task", "title": "Create AD account, mailbox and baseline groups", "group": 102, "dueDays": 1,
             "description": "Created by the HR provisioning job. Confirm employeeID, UPN, manager and department groups. Write the work email back to HR."},
        ]},
        {"name": "Equipment and access", "steps": [
            {"id": "s2", "type": "task", "title": "Provision and enroll device (Autopilot)", "group": 103, "dueDays": 3},
            {"id": "s3", "type": "task", "title": "Badge and building access", "group": 104, "dueDays": 3},
            {"id": "s4", "type": "approval", "title": "Manager confirms role-based access", "approver": "requestor", "dueDays": 2},
        ]},
        {"name": "Day one", "steps": [
            {"id": "s5", "type": "notify", "to": "requestor", "subject": "Day-one checklist", "body": "Account, device and badge are ready for the new hire."},
            {"id": "s6", "type": "webhook", "event": "onboarding.ready"},
            {"id": "s7", "type": "status", "status": 81},
        ]},
    ]}),
    (5002, "Employee Offboarding", "Access removal and equipment recovery, then close.", {"stages": [
        {"name": "Access removal", "steps": [
            {"id": "o1", "type": "task", "title": "Disable account, revoke sessions, strip groups, apply mailbox action", "group": 102, "dueDays": 0},
            {"id": "o2", "type": "task", "title": "Retire device and collect equipment", "group": 103, "dueDays": 5},
            {"id": "o3", "type": "task", "title": "Deactivate badge", "group": 104, "dueDays": 1},
        ]},
        {"name": "Close", "steps": [
            {"id": "o4", "type": "webhook", "event": "offboarding.complete"},
            {"id": "o5", "type": "status", "status": 81},
        ]},
    ]}),
    (5003, "Access Request", "Manager approval, then fulfilment by Identity & Access.", {"stages": [
        {"name": "Approval", "steps": [{"id": "a1", "type": "approval", "title": "Manager approves access", "approver": "requestor_manager"}]},
        {"name": "Fulfil", "steps": [{"id": "a2", "type": "task", "title": "Grant access and confirm with requestor", "group": 102, "dueDays": 2}]},
        {"name": "Close", "steps": [{"id": "a3", "type": "status", "status": 81}]},
    ]}),
]

RULES = [
    (1, "Onboarding intake", {"TypeID": 2001}, {"ResponsibleGroupID": 102, "PriorityID": 2, "StatusID": 78, "StartWorkflowID": 5001,
                                                 "Comment": "Routed to Identity & Access; onboarding workflow started."}),
    (2, "Offboarding intake", {"TypeID": 2002}, {"ResponsibleGroupID": 102, "PriorityID": 3, "StatusID": 78, "StartWorkflowID": 5002,
                                                  "Comment": "Routed to Identity & Access; offboarding workflow started."}),
    (3, "Access request intake", {"TypeID": 2003}, {"ResponsibleGroupID": 102, "StatusID": 78, "StartWorkflowID": 5003}),
    (4, "Hardware request intake", {"TypeID": 2004}, {"ResponsibleGroupID": 103, "StatusID": 78}),
    (5, "Incident intake", {"TypeID": 2005}, {"ResponsibleGroupID": 101, "StatusID": 78}),
]

TECHNICIANS = [  # username, first, last, title, dept, groups, role
    ("svc-hrprov", "HR Provisioning", "(integration)", "Service account", 700, [102], "Technician", tdx.INTEGRATION_UID),
    ("dokafor", "Dana", "Okafor", "Help Desk Technician", 700, [101], "Technician", None),
    ("gbeazer", "Germaine", "Beazer", "Automation Engineer", 700, [102, 101], "Technician", None),
    ("mlindqvist", "Marcus", "Lindqvist", "Desktop Support Technician", 700, [103], "Technician", None),
    ("loyelaran", "Lena", "Oyelaran", "Facilities Coordinator", 400, [104], "Technician", None),
    ("psandoval", "Priya", "Sandoval", "HR Generalist", 800, [105], "Technician", None),
    ("vabernathy", "Victor", "Abernathy", "IT Director", 700, [101, 102, 103], "Admin", None),
]


def seed(conn: sqlite3.Connection, settings: Settings) -> dict:
    if conn.execute("SELECT COUNT(*) FROM ticket_statuses").fetchone()[0]:
        return {"seeded": False}
    conn.executemany("INSERT INTO accounts(ID, Name, Code) VALUES (?, ?, ?)", DEPARTMENTS)
    conn.executemany("INSERT INTO groups_(ID, Name, Description) VALUES (?, ?, ?)", GROUPS)
    conn.executemany("INSERT INTO ticket_statuses(ID, Name, StatusClass, SortOrder) VALUES (?, ?, ?, ?)", STATUSES)
    conn.executemany("INSERT INTO priorities(ID, Name, SortOrder) VALUES (?, ?, ?)", PRIORITIES)
    conn.executemany("INSERT INTO ticket_types(ID, Name, CategoryName) VALUES (?, ?, ?)", TYPES)
    conn.executemany("INSERT INTO forms(ID, Name, TypeID, Attributes) VALUES (?, ?, ?, ?)", [(i, n, t, json.dumps(a)) for i, n, t, a in FORMS])
    conn.executemany("INSERT INTO services(ID, Name, Description, CategoryName, TypeID, FormID) VALUES (?, ?, ?, ?, ?, ?)", SERVICES)
    conn.executemany("INSERT INTO workflows(ID, Name, Description, Definition) VALUES (?, ?, ?, ?)", [(i, n, d, json.dumps(w)) for i, n, d, w in WORKFLOWS])
    conn.executemany("INSERT INTO automation_rules(ID, Name, Conditions, Actions, SortOrder) VALUES (?, ?, ?, ?, ?)",
                     [(i, n, json.dumps(c), json.dumps(a), i) for i, n, c, a in RULES])
    conn.commit()

    uids = {}
    for username, first, last, title, dept, gids, role, fixed_uid in TECHNICIANS:
        uid = tdx.upsert_person(conn, {"UserName": username, "FirstName": first, "LastName": last, "Title": title, "DefaultAccountID": dept,
                                       "PrimaryEmail": f"{username}@harbormain.example", "SecurityRoleName": role, "IsEmployee": 1},
                                uid=fixed_uid or str(uuid.uuid5(uuid.NAMESPACE_URL, f"mock-tdx/tech/{username}")))
        uids[username] = uid
        conn.executemany("INSERT OR IGNORE INTO group_members(GroupID, UID) VALUES (?, ?)", [(g, uid) for g in gids])
    director = uids["vabernathy"]
    for username in ("dokafor", "gbeazer", "mlindqvist"):
        conn.execute("UPDATE people SET ReportsToUID = ? WHERE UID = ?", (director, uids[username]))
    conn.commit()

    # a few tickets so the desktop is not empty
    t1 = tdx.create_ticket(conn, settings, {"TypeID": 2005, "Title": "Teller workstation at Harbor Branch will not boot", "Description": "Blue screen on start, INACCESSIBLE_BOOT_DEVICE.",
                                            "PriorityID": 3, "RequestorUid": uids["psandoval"]}, uids["dokafor"], source="Phone")
    t2 = tdx.create_ticket(conn, settings, {"TypeID": 2004, "FormID": 3004, "Title": "Second monitor for loan processing desk", "PriorityID": 1,
                                            "RequestorUid": uids["psandoval"], "Attributes": [{"ID": 1008, "Value": "Monitor"}]}, uids["psandoval"], source="Portal")
    tdx.update_ticket_fields(conn, settings, t1["ID"], {"ResponsibleUid": uids["mlindqvist"], "StatusID": 79}, uids["dokafor"])
    return {"seeded": True, "people": len(uids), "tickets": 2, "integration_uid": tdx.INTEGRATION_UID}
