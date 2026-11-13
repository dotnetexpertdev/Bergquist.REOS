"""Run Alembic migrations against the configured database, from any directory."""

import sqlite3
from pathlib import Path
from typing import Optional

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from .config import BACKEND_DIR, settings


def alembic_config(database_url: Optional[str] = None) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url or settings.sync_database_url)
    cfg.attributes["configure_logger"] = False
    return cfg


def head_revision() -> str:
    script = ScriptDirectory.from_config(alembic_config())
    return script.get_current_head()


def _legacy_unversioned(db_path: Path) -> bool:
    """True for databases created by the old create_all() code (no alembic_version)."""
    if not db_path.exists():
        return False
    con = sqlite3.connect(str(db_path))
    try:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    return "documents" in names and "alembic_version" not in names


def migrate(db_path: Optional[Path] = None) -> str:
    """Create or upgrade the database. Returns a short description of what happened."""
    db_path = Path(db_path) if db_path else settings.database_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = alembic_config(f"sqlite:///{db_path.as_posix()}")
    note = ""
    if _legacy_unversioned(db_path):
        # The schema matches revision 001 exactly; record that, then upgrade.
        command.stamp(cfg, "001")
        note = " (existing database adopted at revision 001)"
    created = not db_path.exists()
    command.upgrade(cfg, "head")
    return ("created" if created else "upgraded") + note
