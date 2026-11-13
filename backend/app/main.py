"""Morning Assistant POC - FastAPI Application."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import ENV_FILE_REASON, GMAIL_CALLBACK_ROUTE, QUICKBOOKS_CALLBACK_ROUTE, settings
from .database import check_schema, get_db_context
from .routes import auth, documents, gmail, integrations, mappings, properties, quickbooks, search
from .services.accounting import recover_interrupted_operations
from .services.extraction import process_pending_extractions

log = logging.getLogger("morning_assistant")


@asynccontextmanager
async def lifespan(app: FastAPI):
    problems = settings.startup_problems()
    if problems:
        raise RuntimeError("Configuration problem: " + " ".join(problems))
    print(f"Config file : {settings.env_file or '(none)'} [{ENV_FILE_REASON}]")
    print(f"Database    : {settings.database_path}")
    revision = await check_schema()  # never creates a database implicitly
    print(f"Schema      : revision {revision}")
    for path in (settings.documents_path, settings.emails_path, settings.temp_path):
        path.mkdir(parents=True, exist_ok=True)

    async with get_db_context() as db:
        recovered = await recover_interrupted_operations(db)
    if recovered:
        print(f"Recovered {recovered} interrupted QuickBooks operation(s); they need a resume action.")

    async def resume_reading():
        await asyncio.sleep(2)
        async with get_db_context() as db:
            await process_pending_extractions(db)

    task = asyncio.create_task(resume_reading())
    print(f"Owner login : {'configured' if settings.owner_password_hash else 'NOT SET (run cli.py create-owner)'}")
    print(f"Gmail {settings.is_gmail_configured()} | QuickBooks {settings.is_quickbooks_configured()} | "
          f"OpenAI {settings.is_openai_configured()} | Todoist {settings.is_todoist_configured()}")
    print(f"OAuth redirect URIs: {settings.gmail_redirect_uri} | {settings.quickbooks_redirect_uri}")
    yield
    task.cancel()


app = FastAPI(title="Morning Assistant POC", description="Bill intake and unpaid-bill recording",
              version="1.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", settings.public_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (auth.router, documents.router, gmail.router, quickbooks.router, search.router, properties.router,
          integrations.router, mappings.router):
    app.include_router(r)

# If the configured callback paths differ from the canonical routes (for example
# an older .env using /api/auth/gmail/callback), register the same handlers at the
# configured paths so the redirect URI registered with Google/Intuit always works.
if settings.gmail_oauth_callback != GMAIL_CALLBACK_ROUTE:
    app.add_api_route(settings.gmail_oauth_callback, gmail.gmail_callback, methods=["GET"],
                      include_in_schema=False, name="gmail_callback_configured")
if settings.quickbooks_oauth_callback != QUICKBOOKS_CALLBACK_ROUTE:
    app.add_api_route(settings.quickbooks_oauth_callback, quickbooks.quickbooks_callback, methods=["GET"],
                      include_in_schema=False, name="quickbooks_callback_configured")


@app.get("/api/health")
async def health_check():
    return {
        "status": "healthy",
        "gmail_configured": settings.is_gmail_configured(),
        "quickbooks_configured": settings.is_quickbooks_configured(),
        "openai_configured": settings.is_openai_configured(),
        "todoist_configured": settings.is_todoist_configured(),
        "max_upload_size_mb": settings.max_upload_size_mb,
    }


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error. The record was not changed "
                                                            "unless its history says so; try again."})


# Built React app (single origin for API and UI).
frontend_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"

if (frontend_dist / "index.html").exists():
    if (frontend_dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=str(frontend_dist / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        candidate = (frontend_dist / full_path).resolve()
        if full_path and candidate.is_file() and frontend_dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(frontend_dist / "index.html")
else:
    @app.get("/", include_in_schema=False)
    async def root():
        return {"message": "Morning Assistant API is running. Build the UI with: cd frontend; npm run build",
                "docs": "/docs"}
