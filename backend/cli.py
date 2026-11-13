"""CLI commands for Morning Assistant.

Run from any directory, e.g. from the project root:

    python backend\\cli.py doctor
    python backend\\cli.py migrate
    python backend\\cli.py create-owner
"""

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

# Make `app` importable no matter where the CLI is started from.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import ENV_FILE_REASON, settings  # noqa: E402


def create_owner(password_stdin: bool = False) -> None:
    """Print a bcrypt hash for the owner password (never stores it)."""
    from app.auth import PasswordTooLong, hash_password

    if password_stdin:
        username = settings.owner_username
        password = sys.stdin.readline().rstrip("\r\n")
        confirm = password
    else:
        print("Create Owner Account")
        print("=" * 40)
        username = input(f"Username [{settings.owner_username}]: ").strip() or settings.owner_username
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm password: ")

    if not password:
        print("Error: Password cannot be empty", file=sys.stderr)
        sys.exit(1)
    if password != confirm:
        print("Error: Passwords do not match", file=sys.stderr)
        sys.exit(1)
    try:
        password_hash = hash_password(password)
    except PasswordTooLong as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if password_stdin:
        print(password_hash)
        return
    print()
    print(f"Add these lines to {settings.env_file or 'your .env file'}:")
    print()
    print(f"OWNER_USERNAME={username}")
    print(f"OWNER_PASSWORD_HASH={password_hash}")
    print()
    print("Then restart the server.")


def generate_keys() -> None:
    """Generate required secret keys."""
    import secrets
    from cryptography.fernet import Fernet

    print("Add these to your .env file:")
    print()
    print(f"SESSION_SECRET={secrets.token_hex(32)}")
    print(f"TOKEN_ENCRYPTION_KEY={Fernet.generate_key().decode()}")
    print()


def doctor() -> None:
    """Show where configuration and data are read from (no secrets printed)."""
    print(f"Config file      : {settings.env_file or '(none)'}  [{ENV_FILE_REASON}]")
    print(f"Data directory   : {settings.data_path}")
    print(f"Database         : {settings.database_path}  (exists: {settings.database_path.exists()})")
    print(f"Public origin    : {settings.public_origin}")
    print()
    print("Register these exact redirect URIs:")
    print(f"  Google (Gmail) : {settings.gmail_redirect_uri}")
    print(f"  Intuit (QBO)   : {settings.quickbooks_redirect_uri}")
    print()
    print(f"Owner login set  : {'yes' if settings.owner_password_hash else 'NO - run create-owner'}")
    print(f"Gmail            : {'configured' if settings.is_gmail_configured() else 'not configured'}")
    qb = "configured" if settings.is_quickbooks_configured() else "not configured"
    realm = settings.quickbooks_expected_realm_id or "NOT SET (accounting writes are blocked)"
    print(f"QuickBooks       : {qb}; environment={settings.quickbooks_environment}; expected company={realm}")
    print(f"OpenAI           : {'configured' if settings.is_openai_configured() else 'not configured'}")
    print(f"Todoist          : {'enabled' if settings.is_todoist_configured() else 'disabled'}")


def migrate() -> None:
    from app.migrations import migrate as run_migrations

    result = run_migrations()
    print(f"Database {result}: {settings.database_path}")


def serve(host: str, port: int, reload: bool, certfile: str = None, keyfile: str = None) -> None:
    """Run the API and the built React app on one origin."""
    import uvicorn

    kwargs = {}
    if certfile and keyfile:
        kwargs.update(ssl_certfile=certfile, ssl_keyfile=keyfile)
    uvicorn.run("app.main:app", app_dir=str(Path(__file__).resolve().parent), host=host, port=port,
                reload=reload, **kwargs)


async def create_property(name: str, address: str = None) -> None:
    from app.database import check_schema, get_db_context
    from app.models import Property

    await check_schema()
    async with get_db_context() as db:
        prop = Property(name=name, address=address)
        db.add(prop)
        await db.flush()
        print(f"Created property: {prop.id} - {prop.name}")


# Rows that belong to stored documents, children first (foreign keys are enforced).
DOCUMENT_TABLES = ("duplicate_candidates", "accounting_claims", "operations", "audit_events", "arrivals", "documents")


def reset_documents(assume_yes: bool = False, backup: bool = True) -> None:
    """Delete every stored document, its files and history; keep connections, properties and mappings.

    Nothing in QuickBooks or Todoist is touched: bills and tasks created earlier stay there.
    """
    import shutil
    import sqlite3
    from datetime import datetime

    db_path = settings.database_path
    if not db_path.exists():
        print(f"No database at {db_path}; nothing to clean.")
        return
    con = sqlite3.connect(str(db_path), timeout=5)
    try:
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in DOCUMENT_TABLES}
    finally:
        con.close()
    folders = [settings.documents_path, settings.emails_path, settings.temp_path]
    files = sum(1 for f in folders if f.exists() for x in f.rglob("*") if x.is_file())

    print("This deletes:")
    print(f"  {counts['documents']} documents, {counts['arrivals']} arrivals, {counts['audit_events']} history events,")
    print(f"  {counts['operations']} accounting operations, {counts['accounting_claims']} invoice claims, "
          f"{counts['duplicate_candidates']} duplicate suggestions, and {files} stored files.")
    print("It keeps: QuickBooks/Gmail connections, properties, vendor/account mappings.")
    print("QuickBooks bills and Todoist tasks already created are NOT removed.")
    print("Stop the server before running this.")
    if not assume_yes and input("Type DELETE to continue: ").strip() != "DELETE":
        print("Cancelled; nothing was changed.")
        return

    if backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive = shutil.make_archive(str(settings.data_path.parent / f"backup-{stamp}"), "zip",
                                      root_dir=str(settings.data_path))
        print(f"Backup: {archive}")

    con = sqlite3.connect(str(db_path), timeout=5)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        with con:
            con.execute("UPDATE documents SET copy_of_document_id = NULL")
            for table in DOCUMENT_TABLES:
                con.execute(f"DELETE FROM {table}")
        con.execute("VACUUM")
    except sqlite3.OperationalError as exc:
        print(f"Error: {exc}. Is the server still running? Stop it and try again.", file=sys.stderr)
        sys.exit(1)
    finally:
        con.close()
    for folder in folders:
        if folder.exists():
            for child in folder.iterdir():
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    print("Done. All documents were removed; start the server and upload again.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Morning Assistant CLI")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    owner = subparsers.add_parser("create-owner", help="Print an owner password hash for .env")
    owner.add_argument("--password-stdin", action="store_true",
                       help="Read the password from standard input and print only the hash")
    subparsers.add_parser("generate-keys", help="Generate secret keys")
    subparsers.add_parser("doctor", help="Show resolved config, data path and OAuth redirect URIs")
    subparsers.add_parser("migrate", help="Create or upgrade the database (Alembic)")
    subparsers.add_parser("init-db", help="Alias for migrate")

    serve_parser = subparsers.add_parser("serve", help="Run the server (API + built UI)")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Use 0.0.0.0 to allow your phone on the LAN")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    serve_parser.add_argument("--ssl-certfile", help="TLS certificate (e.g. from mkcert) for HTTPS")
    serve_parser.add_argument("--ssl-keyfile", help="TLS private key")

    reset_parser = subparsers.add_parser(
        "reset-documents", help="Delete all stored bills/receipts and their history (keeps connections)")
    reset_parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    reset_parser.add_argument("--no-backup", action="store_true", help="Skip the automatic backup zip")

    property_parser = subparsers.add_parser("create-property", help="Create a property")
    property_parser.add_argument("name", help="Property name")
    property_parser.add_argument("--address", help="Property address")

    args = parser.parse_args()

    if args.command == "create-owner":
        create_owner(args.password_stdin)
    elif args.command == "generate-keys":
        generate_keys()
    elif args.command == "doctor":
        doctor()
    elif args.command in ("migrate", "init-db"):
        migrate()
    elif args.command == "serve":
        serve(args.host, args.port, args.reload, args.ssl_certfile, args.ssl_keyfile)
    elif args.command == "reset-documents":
        reset_documents(args.yes, backup=not args.no_backup)
    elif args.command == "create-property":
        asyncio.run(create_property(args.name, args.address))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
