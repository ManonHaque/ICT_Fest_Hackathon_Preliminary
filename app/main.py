"""CoWork API application entrypoint."""
from fastapi import FastAPI
from fastapi.security import HTTPBearer
from fastapi.openapi.utils import get_openapi

from .database import Base, engine
from .errors import AppError, app_error_handler
from .routers import admin, auth, bookings, health, rooms

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="CoWork API",
    version="1.0.0",
    description="Coworking Space Booking API — register, book rooms, manage cancellations.",
)


def custom_openapi():
    """Inject a Bearer security scheme so Swagger UI shows the Authorize button."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    schema.setdefault("components", {}).setdefault("securitySchemes", {})["HTTPBearer"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "Paste your access_token (without 'Bearer ' prefix).",
    }
    # Tag every protected endpoint with this security scheme so Swagger shows
    # a lock icon next to it.
    bearer = {"HTTPBearer": []}
    public_paths = {"/health", "/auth/register", "/auth/login", "/auth/refresh"}
    for path, ops in schema["paths"].items():
        for method, op in ops.items():
            if isinstance(op, dict) and "operationId" in op:
                if path not in public_paths:
                    op.setdefault("security", []).append(bearer)
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi
app.add_exception_handler(AppError, app_error_handler)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(rooms.router)
app.include_router(bookings.router)
app.include_router(admin.router)