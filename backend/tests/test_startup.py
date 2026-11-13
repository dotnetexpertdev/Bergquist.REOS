"""Item 1/2: the real FastAPI app starts with an isolated database, login works,
password hashing is compatible, and every entry point resolves the same data."""

import os
import subprocess
import sys
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password, verify_password
from app.config import settings
from app.main import app

from .conftest import TEST_PASSWORD

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent


def test_real_app_starts_and_owner_login():
    """Imports app.main.app, runs its lifespan against the migrated temp DB."""
    with TestClient(app) as c:  # runs startup: config checks, schema check, recovery
        assert c.get("/api/health").json()["status"] == "healthy"
        assert c.get("/api/documents").status_code == 401  # protected before login
        bad = c.post("/api/auth/login", json={"username": "owner", "password": "wrong"})
        assert bad.status_code == 401
        bad_user = c.post("/api/auth/login", json={"username": "someone", "password": TEST_PASSWORD})
        assert bad_user.status_code == 401
        ok = c.post("/api/auth/login", json={"username": "owner", "password": TEST_PASSWORD})
        assert ok.status_code == 200 and ok.json()["csrf_token"]
        assert c.get("/api/documents").status_code == 200
        session = c.get("/api/auth/session").json()
        assert session["authenticated"] and session["username"] == "owner"


def test_hash_password_and_verify():
    h = hash_password("s3cret-pass")
    assert h.startswith("$2b$12$")
    assert verify_password("s3cret-pass", h)
    assert not verify_password("s3cret-pasS", h)
    assert not verify_password("anything", "")
    assert not verify_password("anything", "not-a-hash")


@pytest.mark.parametrize("prefix", ["$2a$", "$2b$", "$2y$"])
def test_existing_passlib_style_hashes_still_verify(prefix):
    """Hashes created earlier by Passlib (bcrypt 2a/2b, or 2y) keep working."""
    stored = bcrypt.hashpw(b"legacy-password", bcrypt.gensalt(rounds=4, prefix=b"2b")).decode()
    stored = prefix + stored[4:]
    assert verify_password("legacy-password", stored)
    assert not verify_password("legacy-passwordX", stored)


def test_published_bcrypt_test_vector():
    # Openwall crypt_blowfish reference vector ($2a$, cost 5): password "U*U".
    assert verify_password("U*U", "$2a$05$CCCCCCCCCCCCCCCCCCCCC.E5YPO9kmyuRGyh0XouQYb4YMJKvyOeW")
    assert not verify_password("U*V", "$2a$05$CCCCCCCCCCCCCCCCCCCCC.E5YPO9kmyuRGyh0XouQYb4YMJKvyOeW")


def _env(**extra):
    env = dict(os.environ)
    env.update(extra)
    return env


def test_cli_create_owner_generates_working_hash():
    """The documented owner-creation command works on this install (bcrypt 5, no Passlib)."""
    out = subprocess.run([sys.executable, str(BACKEND / "cli.py"), "create-owner", "--password-stdin"],
                         input="Owner-Pass-123\n", capture_output=True, text=True, env=_env(), cwd=str(PROJECT),
                         timeout=60)
    assert out.returncode == 0, out.stderr
    h = out.stdout.strip()
    assert h.startswith("$2b$")
    assert verify_password("Owner-Pass-123", h)
    assert not verify_password("owner-pass-123", h)


@pytest.mark.parametrize("cwd", [PROJECT, BACKEND, BACKEND / "app"])
def test_cli_resolves_same_database_from_any_directory(cwd):
    out = subprocess.run([sys.executable, str(BACKEND / "cli.py"), "doctor"], capture_output=True, text=True,
                         env=_env(), cwd=str(cwd), timeout=60)
    assert out.returncode == 0, out.stderr
    assert str(settings.database_path) in out.stdout
    assert "http://testserver/api/gmail/callback" in out.stdout
    assert "http://testserver/api/quickbooks/callback" in out.stdout


def test_relative_data_path_is_relative_to_env_file(tmp_path):
    env_dir = tmp_path / "cfg"
    env_dir.mkdir()
    bom = "﻿"  # Notepad-style byte-order mark must be tolerated
    (env_dir / ".env").write_text(bom + "SESSION_SECRET=" + "x" * 40 + "\nDATA_PATH=./mydata\n", encoding="utf-8")
    code = f"import sys; sys.path.insert(0, r'{BACKEND}'); from app.config import settings; print(settings.database_path)"
    env = _env(MORNING_ASSISTANT_ENV_FILE=str(env_dir / ".env"))
    env.pop("DATA_PATH")
    seen = set()
    for cwd in (PROJECT, BACKEND, tmp_path):
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                             cwd=str(cwd), timeout=60)
        assert out.returncode == 0, out.stderr
        seen.add(Path(out.stdout.strip()))
    assert seen == {(env_dir / "mydata" / "morning_assistant.db").resolve()}


def test_server_refuses_missing_database_instead_of_creating_one(tmp_path):
    """Starting with a DATA_PATH that has no migrated DB must fail loudly, not create an empty DB."""
    empty = tmp_path / "nowhere"
    code = ("from fastapi.testclient import TestClient\n"
            "from app.main import app\n"
            "try:\n"
            "    with TestClient(app):\n"
            "        print('STARTED')\n"
            "except Exception as e:\n"
            "    print('REFUSED', e)\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=_env(DATA_PATH=str(empty)), cwd=str(BACKEND), timeout=60)
    assert "REFUSED" in out.stdout and "cli.py migrate" in out.stdout, out.stdout + out.stderr
    assert not (empty / "morning_assistant.db").exists()


def test_migrate_creates_empty_database(tmp_path):
    out = subprocess.run([sys.executable, str(BACKEND / "cli.py"), "migrate"], capture_output=True, text=True,
                         env=_env(DATA_PATH=str(tmp_path / "fresh")), cwd=str(PROJECT), timeout=120)
    assert out.returncode == 0, out.stderr
    import sqlite3
    con = sqlite3.connect(tmp_path / "fresh" / "morning_assistant.db")
    assert con.execute("select version_num from alembic_version").fetchone()[0] == "003"
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    assert {"documents", "arrivals", "accounting_claims", "operations"} <= tables
