"""Drives the mock the way the HR provisioning job will drive the real tenant."""
import json


def _admin(client):
    r = client.post("/TDWebApi/api/auth/loginadmin", json={"BEID": "7f2c1a44-6c3e-4b7b-9a1e-2d5f8c0b1e21", "WebServicesKey": "test-key"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    return {"Authorization": f"Bearer {r.text}"}


def test_auth_paths(client):
    assert client.post("/TDWebApi/api/auth/loginadmin", json={"BEID": "x", "WebServicesKey": "wrong"}).status_code == 401
    assert client.get("/TDWebApi/api/431/tickets/statuses").status_code == 401
    h = _admin(client)
    assert client.get("/TDWebApi/api/auth/getuser", headers=h).json()["FullName"].startswith("HR Provisioning")
    r = client.post("/TDWebApi/api/auth", json={"username": "gbeazer", "password": "test-pass"})
    assert r.status_code == 200
    me = client.get("/TDWebApi/api/auth/getuser", headers={"Authorization": f"Bearer {r.text}"}).json()
    assert me["UserName"] == "gbeazer" and 102 in me["GroupIDs"]
    assert client.get("/TDWebApi/api/999/tickets/statuses", headers=h).status_code == 404


def test_people_import_then_onboarding_ticket_runs_the_workflow(client, roster):
    h = _admin(client)
    # 1. the HR roster feeds People (TDX People Import)
    r = client.post("/TDWebApi/api/people/import", headers=h, content=roster.encode())
    assert r.status_code == 200
    res = r.json()
    assert res["created"] == 4 and res["errors"] == []
    mgr = client.post("/TDWebApi/api/people/search", headers=h, json={"ExternalID": "K3ZP8W2RD"}).json()[0]
    assert mgr["PrimaryEmail"] == "rosa.sandoval@harbormain.example" and mgr["ReportsToFullName"] == "Bennett Abernathy"
    leaver = client.post("/TDWebApi/api/people/search", headers=h, json={"ExternalID": "X2V4UK8LV", "IsActive": False}).json()
    assert len(leaver) == 1 and leaver[0]["IsActive"] is False
    # re-import: updates, no duplicates, name change keeps the record
    r2 = client.post("/TDWebApi/api/people/import", headers=h, content=roster.replace("Quiroga,Keiko", "Nakagawa,Keiko").encode()).json()
    assert r2["created"] == 0 and r2["updated"] == 4
    assert client.post("/TDWebApi/api/people/search", headers=h, json={"ExternalID": "G7Q2M4K1A"}).json()[0]["LastName"] == "Nakagawa"

    # 2. idempotency check the processor does before creating: search by type + Associate ID attribute
    found = client.post("/TDWebApi/api/431/tickets/search", headers=h, json={"TypeIDs": [2001], "CustomAttributes": [{"ID": 1001, "Value": "G7Q2M4K1A"}]}).json()
    assert found == []

    # 3. create the onboarding ticket the way the processor will
    body = {"TypeID": 2001, "FormID": 3001, "Title": "Onboarding: Quiroga, Keiko (G7Q2M4K1A) starts 2026-11-13",
            "Description": "Created by the HR provisioning job. Run id 2026-11-03-01.", "PriorityID": 2, "RequestorUid": mgr["UID"],
            "Attributes": [{"ID": 1001, "Value": "G7Q2M4K1A"}, {"ID": 1002, "Value": "2026-11-13"}, {"ID": 1003, "Value": "Teller"},
                           {"ID": 1004, "Value": "000200 Retail Banking"}, {"ID": 1005, "Value": "Harbor Branch"}, {"ID": 1006, "Value": "Sandoval, Rosa"},
                           {"ID": 1007, "Value": "Part Time"}, {"ID": 1008, "Value": "Standard laptop"}]}
    r = client.post("/TDWebApi/api/431/tickets?EnableNotifications=true&NotifyRequestor=false", headers=h, json=body)
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["ID"] >= 10001 and t["TypeName"] == "Employee Onboarding" and t["AccountName"] == "Retail Banking"
    # automation rule: routed to Identity & Access, status Open, workflow started
    assert t["ResponsibleGroupName"] == "Identity & Access" and t["StatusName"] == "Open" and t["WorkflowName"] == "Employee Onboarding"
    assert any(a["Name"] == "Location" and a["ValueText"] == "Harbor Branch" for a in t["Attributes"])
    found = client.post("/TDWebApi/api/431/tickets/search", headers=h, json={"TypeIDs": [2001], "CustomAttributes": [{"ID": 1001, "Value": "G7Q2M4K1A"}]}).json()
    assert [x["ID"] for x in found] == [t["ID"]]

    # 4. stage 1 task exists for Identity & Access; completing it opens stage 2 (two tasks + manager approval)
    tasks = client.get(f"/TDWebApi/api/431/tickets/{t['ID']}/tasks", headers=h).json()
    assert [x["WorkflowStepID"] for x in tasks] == ["s1"] and tasks[0]["ResponsibleGroupName"] == "Identity & Access"
    r = client.post(f"/TDWebApi/api/431/tickets/{t['ID']}/tasks/{tasks[0]['ID']}/feed", headers=h, json={"PercentComplete": 100, "Comments": "AD account HMB\\kquiroga created; mailbox pending licence."})
    assert r.status_code == 201 and r.json()["PercentComplete"] == 100
    wf = client.get(f"/TDWebApi/api/431/tickets/{t['ID']}/workflow", headers=h).json()
    assert wf["CurrentStage"] == 1 and wf["Status"] == "InProgress"
    tasks = client.get(f"/TDWebApi/api/431/tickets/{t['ID']}/tasks", headers=h).json()
    assert sorted(x["WorkflowStepID"] for x in tasks) == ["s1", "s2", "s3"]
    approvals = [st for st in wf["Stages"][1]["steps"] if st["type"] == "approval"]
    assert approvals and approvals[0]["state"] == "pending"

    # 5. finish stage 2: both tasks + approval -> stage 3 runs notify/webhook/status and completes
    for x in tasks:
        if x["PercentComplete"] < 100:
            client.put(f"/TDWebApi/api/431/tickets/{t['ID']}/tasks/{x['ID']}", headers=h, json={"PercentComplete": 100})
    wf = client.get(f"/TDWebApi/api/431/tickets/{t['ID']}/workflow", headers=h).json()
    assert wf["CurrentStage"] == 1  # approval still pending
    from mocktdx import db, tdx
    conn = db.connect(client.app.state.settings.db_path)
    appr = tdx.approvals(conn, t["ID"])[0]
    conn.close()
    r = client.post(f"/TDWebApi/api/431/tickets/{t['ID']}/workflow/approvals/{appr['ID']}", headers=h, json={"Approve": True, "Comments": "Baseline access is right."})
    assert r.status_code == 200 and r.json()["Status"] == "Completed"
    t2 = client.get(f"/TDWebApi/api/431/tickets/{t['ID']}", headers=h).json()
    assert t2["StatusName"] == "Resolved" and t2["CompletedDate"]
    feed = client.get(f"/TDWebApi/api/431/tickets/{t['ID']}/feed", headers=h).json()
    assert any("Notification sent to rosa.sandoval@harbormain.example" in e["Body"] for e in feed)
    assert any("onboarding.ready" in e["Body"] for e in feed)

    # 6. comment with status change through the feed, and JSON Patch
    r = client.post(f"/TDWebApi/api/431/tickets/{t['ID']}/feed", headers=h, json={"Comments": "Closing after day one.", "NewStatusID": 82, "IsPrivate": False})
    assert r.status_code == 201
    r = client.patch(f"/TDWebApi/api/431/tickets/{t['ID']}", headers=h, json=[{"op": "replace", "path": "/PriorityID", "value": 1}])
    assert r.status_code == 200 and r.json()["PriorityName"] == "Low" and r.json()["StatusName"] == "Closed"
    assert client.patch(f"/TDWebApi/api/431/tickets/{t['ID']}", headers=h, json=[{"op": "replace", "path": "/Nope", "value": 1}]).status_code == 400

    # 7. the API audit log recorded the integration account
    assert "HR Provisioning (integration)" in client.get("/TDAdmin?tab=log").text


def test_people_import_accepts_curl_default_content_type_and_multipart(client, roster):
    h = _admin(client)
    r = client.post("/TDWebApi/api/people/import", headers={**h, "content-type": "application/x-www-form-urlencoded"}, content=roster.encode())
    assert r.status_code == 200 and r.json()["created"] == 4
    r = client.post("/TDWebApi/api/people/import", headers=h, files={"file": ("roster.csv", roster.encode(), "text/csv")})
    assert r.status_code == 200 and r.json()["updated"] == 4 and r.json()["created"] == 0
    assert client.post("/TDWebApi/api/people/import", headers=h, files={"other": ("x.csv", b"a,b", "text/csv")}).status_code == 400


def test_offboarding_and_rejected_approval(client, roster):
    h = _admin(client)
    client.post("/TDWebApi/api/people/import", headers=h, content=roster.encode())
    mgr = client.post("/TDWebApi/api/people/search", headers=h, json={"ExternalID": "M425VDGBK"}).json()[0]
    t = client.post("/TDWebApi/api/431/tickets", headers=h, json={"TypeID": 2002, "FormID": 3002, "Title": "Offboarding: Farrow, Umar (X2V4UK8LV)", "RequestorUid": mgr["UID"],
                                                               "Attributes": [{"ID": 1001, "Value": "X2V4UK8LV"}, {"ID": 1010, "Value": "2026-10-22"}, {"ID": 1012, "Value": "Forward to manager"}]}).json()
    assert t["PriorityName"] == "High" and t["WorkflowName"] == "Employee Offboarding"
    assert client.post("/TDWebApi/api/431/tickets", headers=h, json={"TypeID": 2002, "FormID": 3002, "Title": "bad choice", "Attributes": [{"ID": 1012, "Value": "Delete everything"}]}).status_code == 400
    # access request: manager rejects -> workflow cancelled, ticket on hold
    a = client.post("/TDWebApi/api/431/tickets", headers=h, json={"TypeID": 2003, "FormID": 3003, "Title": "Core banking admin", "RequestorUid": mgr["UID"],
                                                               "Attributes": [{"ID": 1014, "Value": "Core banking"}, {"ID": 1016, "Value": "I need it"}]}).json()
    from mocktdx import db, tdx
    conn = db.connect(client.app.state.settings.db_path)
    appr = tdx.approvals(conn, a["ID"])[0]
    conn.close()
    wf = client.post(f"/TDWebApi/api/431/tickets/{a['ID']}/workflow/approvals/{appr['ID']}", headers=h, json={"Approve": False, "Comments": "Not for this role."}).json()
    assert wf["Status"] == "Cancelled"
    assert client.get(f"/TDWebApi/api/431/tickets/{a['ID']}", headers=h).json()["StatusName"] == "On Hold"


def test_ui_pages_render(client, roster):
    h = _admin(client)
    client.post("/TDWebApi/api/people/import", headers=h, content=roster.encode())
    for url in ("/TDNext", "/TDNext/Apps/431/Tickets", "/TDNext/Apps/431/Tickets?view=all&q=monitor", "/TDNext/Apps/431/Tickets/New?type=2001",
                "/TDNext/Apps/431/Tickets/10001", "/TDNext/Apps/People", "/TDNext/Apps/People?q=Sandoval", "/TDAdmin", "/TDAdmin?tab=forms",
                "/TDAdmin?tab=workflows", "/TDAdmin?tab=api", "/TDAdmin?tab=log", "/TDClient", "/TDClient/Requests/ServiceDet?ID=7003", "/TDClient/Requests/TicketRequests"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert "Not affiliated" in r.text
    assert client.get("/", follow_redirects=False).status_code == 307
    # portal request with a required attribute missing -> 400, then a good one -> ticket + workflow
    r = client.post("/TDClient/Requests/ServiceDet", data={"service_id": "7003", "title": "Access to reporting", "attr_1014": "Reporting"}, headers={"accept": "text/html"})
    assert r.status_code == 400 and "Business Justification is required" in r.text  # error page renders
    r = client.post("/TDClient/Requests/ServiceDet", data={"service_id": "7003", "title": "Access to reporting", "attr_1014": "Reporting", "attr_1016": "Month-end reports"}, follow_redirects=False)
    assert r.status_code == 303 and "TicketDet?TicketID=" in r.headers["location"]
    tid = r.headers["location"].split("TicketID=")[1].split("&")[0]
    assert client.get(f"/TDClient/Requests/TicketRequests/TicketDet?TicketID={tid}").status_code == 200
    assert client.get(f"/TDNext/Apps/431/Tickets/{tid}").status_code == 200
    # agent completes a task from the UI
    r = client.post("/TDNext/Apps/431/Tickets/New", data={"type_id": "2004", "form_id": "3004", "title": "Headset", "priority_id": "1", "attr_1008": "Headset"}, follow_redirects=False)
    assert r.status_code == 303
    uid = client.get("/TDNext/Apps/People?q=Sandoval").text.split('/TDNext/Apps/People/')[1][:36]
    assert client.get(f"/TDNext/Apps/People/{uid}").status_code == 200
