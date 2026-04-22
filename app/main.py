from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import api_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="FHIR Agent Service",
        version="0.1.0",
        description="MIMIC-to-FHIR multi-agent web service backed by a local Ollama pipeline.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router)

    @app.get("/", include_in_schema=False)
    def root() -> JSONResponse:
        return JSONResponse(
            {
                "service": "FHIR Agent Service",
                "status": "ok",
                "frontend_url": "http://localhost:3000",
                "available_routes": ["/analyze", "/mapping", "/schema"],
            }
        )

    return app


app = create_app()
