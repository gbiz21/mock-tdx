# mock-tdx

A stand-in for TeamDynamix (TDX) with one job: let you build and test the **ticketing half of HR-driven
onboarding and offboarding** against the same API shapes the real tenant exposes, before you have access to it.

It has what the design needs and not much more:

- A ticketing application (ID 431) with types, statuses, priorities, forms with custom attributes, and services.
- **Ticket tasks**, a **feed** (comments and status changes), and **approvals**.
- A **workflow engine**: stages of task / approval / notification / web-service / status steps. A stage completes
  when its tasks are done and its approvals decided; a rejection cancels the workflow and parks the ticket On Hold.
- **Automation rules** on ticket creation: route to a group, set priority and status, start a workflow.
- **People** with a **People Import** that takes the HR roster CSV (match on Associate ID, deactivate leavers,
  resolve managers), the way TDX's scheduled People Import keeps requestors current.
- A **Web API at `/TDWebApi/api`** with TDX's endpoint paths, auth flow and JSON field names (subset), plus an
  API audit log you can read in Admin.
- Screens: an agent interface (Desktop, IT Tickets, People, Admin) and a client portal (service catalog, my
  requests, approvals).

All data is synthetic. Not affiliated with TeamDynamix; the footer says so.

## Quick start

```bash
cd ~/05_Projects/mock-tdx
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env            # set TDX_WEB_SERVICES_KEY and TDX_TECH_PASSWORD to real random values
.venv/bin/python -m mocktdx serve        # http://127.0.0.1:8090/TDNext
.venv/bin/python -m mocktdx import-people ../mock-adp/outbox/HMB_Employee_Roster_*.csv   # requestors from the HR feed
.venv/bin/python -m pytest -q
```

Open Desktop, switch "Acting as" to any technician, create an **Employee Onboarding** ticket, and watch the
automation rule route it and the workflow create the first task. Complete tasks, approve as the manager in the
client portal, and the ticket resolves itself.

## How the processor uses it

This is step 6 of the design: the HR provisioning job opens the ticket that carries the human tasks. The same
PowerShell runs against the real tenant with a different base URL and key.

```powershell
$base = "https://itsm.lab.bkre8tive.com/TDWebApi/api"
$token = Invoke-RestMethod -Method Post -Uri "$base/auth/loginadmin" -ContentType 'application/json' `
          -Body (@{ BEID = $env:TDX_BEID; WebServicesKey = $env:TDX_WEB_SERVICES_KEY } | ConvertTo-Json)
$h = @{ Authorization = "Bearer $token" }

# idempotency: one ticket per Associate ID per onboarding
$existing = Invoke-RestMethod -Method Post -Uri "$base/431/tickets/search" -Headers $h -ContentType 'application/json' `
            -Body (@{ TypeIDs = @(2001); CustomAttributes = @(@{ ID = 1001; Value = $row.'Associate ID' }) } | ConvertTo-Json -Depth 5)
if (-not $existing) {
  $mgr = Invoke-RestMethod -Method Post -Uri "$base/people/search" -Headers $h -ContentType 'application/json' `
         -Body (@{ ExternalID = $row.'Reports To Associate ID' } | ConvertTo-Json)
  $body = @{
    TypeID = 2001; FormID = 3001; PriorityID = 2; RequestorUid = $mgr[0].UID
    Title = "Onboarding: $($row.'Last Name'), $($row.'First Name') ($($row.'Associate ID')) starts $($row.'Hire Date')"
    Description = "Created by the HR provisioning job. Run id $RunId."
    Attributes = @(
      @{ ID = 1001; Value = $row.'Associate ID' }, @{ ID = 1002; Value = $hireDate.ToString('yyyy-MM-dd') },
      @{ ID = 1003; Value = $row.'Job Title Description' }, @{ ID = 1004; Value = "$($row.'Home Department Code') $($row.'Home Department Description')" },
      @{ ID = 1005; Value = $row.'Location Description' }, @{ ID = 1006; Value = $row.'Reports To Name' },
      @{ ID = 1007; Value = $row.'Worker Category Description' }, @{ ID = 1008; Value = 'Standard laptop' })
  } | ConvertTo-Json -Depth 5
  $ticket = Invoke-RestMethod -Method Post -Uri "$base/431/tickets?EnableNotifications=true&NotifyRequestor=false" -Headers $h -ContentType 'application/json' -Body $body
}
# later, when the account exists:
Invoke-RestMethod -Method Post -Uri "$base/431/tickets/$($ticket.ID)/tasks/$($task.ID)/feed" -Headers $h -ContentType 'application/json' `
  -Body (@{ PercentComplete = 100; Comments = "AD account $sam created; mailbox follows the licence. Run id $RunId." } | ConvertTo-Json)
```

## What maps to the real TeamDynamix

| Real TDX | Here | Notes |
| --- | --- | --- |
| `POST /TDWebApi/api/auth/loginadmin` `{BEID, WebServicesKey}` → JWT text | same | key from env; `python -m mocktdx token` prints one |
| `POST /TDWebApi/api/auth` `{username, password}` | same | seeded technicians share `TDX_TECH_PASSWORD` |
| `POST /api/{appId}/tickets`, `GET …/{id}`, `PATCH …/{id}` (JSON Patch), `POST …/search` | same | search supports `CustomAttributes`, which is how the processor stays idempotent |
| `…/tickets/{id}/feed`, `…/tasks`, `…/tasks/{taskId}/feed {PercentComplete}` | same | |
| `…/tickets/{id}/workflow` and approvals | approximated | real TDX has a richer workflow API; the shapes here cover what the design uses |
| `POST /api/people/import` (Excel) + scheduled People Import from SFTP | CSV upload or API | match on ExternalID = HR Associate ID |
| Admin → Ticketing app → Types, Statuses, Forms, Services, Workflows, Automation Rules | Admin tab, seeded, read-only | edit `mocktdx/seed.py` to change them |
| TDNext (agents) and TDClient (portal) | the two screens | no login screen in the lab; a cookie picks who you act as |

Rate limiting, SLAs, knowledge base, assets and projects are not modelled.

## Repo map

| Path | Holds |
| --- | --- |
| `mocktdx/tdx.py` | People, tickets, tasks, feed, approvals, workflow engine, automation rules, auth |
| `mocktdx/api.py` | The Web API router at `/TDWebApi/api` and the audit-log middleware |
| `mocktdx/web.py`, `templates/` | Agent interface, admin, client portal |
| `mocktdx/seed.py` | Departments, groups, statuses, forms, services, the three workflows, five rules, technicians |
| `deploy/` | systemd unit, install script, Traefik route, hosting runbook |
