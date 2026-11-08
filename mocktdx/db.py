"""SQLite storage. Column names follow the TDX Web API field names so serialization is a thin layer."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    ID INTEGER PRIMARY KEY, Name TEXT NOT NULL, Code TEXT NOT NULL DEFAULT '', IsActive INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS groups_ (
    ID INTEGER PRIMARY KEY, Name TEXT NOT NULL UNIQUE, Description TEXT NOT NULL DEFAULT '', IsActive INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS people (
    UID TEXT PRIMARY KEY, UserName TEXT NOT NULL UNIQUE, FirstName TEXT NOT NULL, LastName TEXT NOT NULL,
    PrimaryEmail TEXT NOT NULL DEFAULT '', AlternateEmail TEXT NOT NULL DEFAULT '', Title TEXT NOT NULL DEFAULT '',
    DefaultAccountID INTEGER, ReportsToUID TEXT, IsActive INTEGER NOT NULL DEFAULT 1, IsEmployee INTEGER NOT NULL DEFAULT 1,
    ExternalID TEXT NOT NULL DEFAULT '', SecurityRoleName TEXT NOT NULL DEFAULT 'Client', WorkPhone TEXT NOT NULL DEFAULT '',
    LocationName TEXT NOT NULL DEFAULT '', CreatedDate TEXT NOT NULL, ModifiedDate TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_people_ext ON people(ExternalID);
CREATE INDEX IF NOT EXISTS idx_people_email ON people(PrimaryEmail);
CREATE TABLE IF NOT EXISTS group_members (GroupID INTEGER NOT NULL, UID TEXT NOT NULL, PRIMARY KEY (GroupID, UID));
CREATE TABLE IF NOT EXISTS ticket_types (
    ID INTEGER PRIMARY KEY, Name TEXT NOT NULL, CategoryName TEXT NOT NULL DEFAULT '', IsActive INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS ticket_statuses (
    ID INTEGER PRIMARY KEY, Name TEXT NOT NULL, StatusClass TEXT NOT NULL, SortOrder INTEGER NOT NULL DEFAULT 0, IsActive INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS priorities (ID INTEGER PRIMARY KEY, Name TEXT NOT NULL, SortOrder INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS forms (ID INTEGER PRIMARY KEY, Name TEXT NOT NULL, TypeID INTEGER, Attributes TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS services (
    ID INTEGER PRIMARY KEY, Name TEXT NOT NULL, Description TEXT NOT NULL DEFAULT '', CategoryName TEXT NOT NULL DEFAULT '',
    TypeID INTEGER, FormID INTEGER, IsActive INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS workflows (
    ID INTEGER PRIMARY KEY, Name TEXT NOT NULL, Description TEXT NOT NULL DEFAULT '', Definition TEXT NOT NULL, IsActive INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS automation_rules (
    ID INTEGER PRIMARY KEY, Name TEXT NOT NULL, Trigger TEXT NOT NULL DEFAULT 'TicketCreated', Conditions TEXT NOT NULL DEFAULT '{}',
    Actions TEXT NOT NULL DEFAULT '{}', SortOrder INTEGER NOT NULL DEFAULT 0, IsActive INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS tickets (
    ID INTEGER PRIMARY KEY AUTOINCREMENT, AppID INTEGER NOT NULL, TypeID INTEGER NOT NULL, FormID INTEGER, ServiceID INTEGER,
    Title TEXT NOT NULL, Description TEXT NOT NULL DEFAULT '', AccountID INTEGER, StatusID INTEGER NOT NULL, PriorityID INTEGER NOT NULL,
    RequestorUid TEXT, ResponsibleUid TEXT, ResponsibleGroupID INTEGER, CreatedUid TEXT, CreatedDate TEXT NOT NULL,
    ModifiedUid TEXT, ModifiedDate TEXT NOT NULL, StartDate TEXT, EndDate TEXT, CompletedDate TEXT,
    Attributes TEXT NOT NULL DEFAULT '[]', SourceName TEXT NOT NULL DEFAULT 'Web Service', WorkflowInstanceID INTEGER);
CREATE TABLE IF NOT EXISTS ticket_tasks (
    ID INTEGER PRIMARY KEY AUTOINCREMENT, TicketID INTEGER NOT NULL, Title TEXT NOT NULL, Description TEXT NOT NULL DEFAULT '',
    ResponsibleGroupID INTEGER, ResponsibleUid TEXT, StartDate TEXT, EndDate TEXT, PercentComplete INTEGER NOT NULL DEFAULT 0,
    CompletedDate TEXT, CompletedUid TEXT, SortOrder INTEGER NOT NULL DEFAULT 0, WorkflowStepID TEXT,
    CreatedDate TEXT NOT NULL, ModifiedDate TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ticket_feed (
    ID INTEGER PRIMARY KEY AUTOINCREMENT, TicketID INTEGER NOT NULL, TaskID INTEGER, CreatedUid TEXT,
    CreatedFullName TEXT NOT NULL DEFAULT '', CreatedDate TEXT NOT NULL, Body TEXT NOT NULL DEFAULT '',
    IsPrivate INTEGER NOT NULL DEFAULT 0, OldStatus TEXT, NewStatus TEXT, EntryType TEXT NOT NULL DEFAULT 'Comment');
CREATE TABLE IF NOT EXISTS workflow_instances (
    ID INTEGER PRIMARY KEY AUTOINCREMENT, TicketID INTEGER NOT NULL, WorkflowID INTEGER NOT NULL,
    CurrentStage INTEGER NOT NULL DEFAULT -1, Status TEXT NOT NULL DEFAULT 'InProgress', StartedDate TEXT NOT NULL, CompletedDate TEXT);
CREATE TABLE IF NOT EXISTS approvals (
    ID INTEGER PRIMARY KEY AUTOINCREMENT, TicketID INTEGER NOT NULL, InstanceID INTEGER NOT NULL, StepID TEXT NOT NULL,
    Title TEXT NOT NULL, ApproverUid TEXT, Status TEXT NOT NULL DEFAULT 'Pending', DecidedDate TEXT, DecidedUid TEXT,
    Comment TEXT NOT NULL DEFAULT '', CreatedDate TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS api_log (
    ID INTEGER PRIMARY KEY AUTOINCREMENT, Date TEXT NOT NULL, Method TEXT NOT NULL, Path TEXT NOT NULL,
    Status INTEGER NOT NULL, Caller TEXT NOT NULL DEFAULT '', DurationMs INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS people_imports (
    ID INTEGER PRIMARY KEY AUTOINCREMENT, Date TEXT NOT NULL, Source TEXT NOT NULL, Rows INTEGER, Created INTEGER,
    Updated INTEGER, Deactivated INTEGER, Errors TEXT NOT NULL DEFAULT '');
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    if conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'tickets'").fetchone() is None:
        conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('tickets', 10000)")  # TDX ticket IDs look like 10001
        conn.commit()
    return conn


def is_seeded(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT COUNT(*) FROM ticket_statuses").fetchone()[0] > 0
