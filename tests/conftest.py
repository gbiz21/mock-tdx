import os
from pathlib import Path

import pytest

ROSTER = """Associate ID,File Number,Payroll Company Code,Position ID,Last Name,First Name,Middle Initial,Preferred Name,Work Contact: Work Email,Job Title Description,Home Department Code,Home Department Description,Location Description,Business Unit Description,Worker Category Description,Reports To Associate ID,Reports To Name,Hire Date,Rehire Date,Termination Date,Position Status,Leave Start Date
M425VDGBK,1001,HMB,HMB000001,Abernathy,Bennett,,,bennett.abernathy@harbormain.example,Chief Executive Officer,000100,Executive,Main Branch,Corporate,Full Time,,,03/03/2014,,,Active,
K3ZP8W2RD,1005,HMB,HMB000005,Sandoval,Rosa,L,Rosie,rosa.sandoval@harbormain.example,Branch Manager,000200,Retail Banking,Main Branch,Member Services,Full Time,M425VDGBK,"Abernathy, Bennett",05/18/2016,,,Active,
G7Q2M4K1A,1042,HMB,HMB000042,Quiroga,Keiko,,,,Teller,000200,Retail Banking,Harbor Branch,Member Services,Part Time,K3ZP8W2RD,"Sandoval, Rosa",11/13/2026,,,Active,
X2V4UK8LV,1030,HMB,HMB000030,Farrow,Umar,,,umar.farrow@harbormain.example,Loan Processor,000300,Lending,Main Branch,Member Services,Full Time,M425VDGBK,"Abernathy, Bennett",02/01/2019,,10/22/2026,Terminated,
"""


@pytest.fixture
def settings(tmp_path: Path):
    env = {"DB_PATH": str(tmp_path / "t.db"), "TDX_WEB_SERVICES_KEY": "test-key", "TDX_TECH_PASSWORD": "test-pass",
           "TDX_TOKEN_SECRET": "unit-test-secret", "WEBHOOK_URL": ""}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    from mocktdx import settings as settings_module
    try:
        yield settings_module.load(env_file=tmp_path / "no.env")
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def client(settings):
    import importlib
    from fastapi.testclient import TestClient
    web = importlib.reload(importlib.import_module("mocktdx.web"))
    with TestClient(web.app) as c:
        yield c


@pytest.fixture
def roster() -> str:
    return ROSTER
