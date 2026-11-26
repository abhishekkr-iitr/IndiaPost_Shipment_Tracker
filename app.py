"""Local browser application entry point for India Post AI Shipment Assistant."""

from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


def load_application_environment() -> bool:
    """Load this project's .env without overriding existing OS environment values."""
    return load_dotenv(dotenv_path=BASE_DIR / ".env", override=False)


# Load configuration before importing modules that may read environment values.
load_application_environment()

from api.conversation_routes import router as conversation_router  # noqa: E402
from api.auth_routes import router as auth_router  # noqa: E402
from api.guest_routes import router as guest_router  # noqa: E402
from api.telegram_routes import router as telegram_router  # noqa: E402

app = FastAPI(
    title="India Post AI Shipment Assistant",
    description="Local browser interface for the existing shipment assistant.",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.include_router(conversation_router)
app.include_router(auth_router)
app.include_router(guest_router)
app.include_router(telegram_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", include_in_schema=False)
def health():
    # Process-level health only; deliberately does not call external providers.
    return {"status": "ok", "application": "india-post-ai-shipment-assistant"}
