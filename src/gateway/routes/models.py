"""
GET /v1/models — OpenAI-compatible model listing endpoint.

Returns a single static model entry representing the codex-cli backend.
Auth is enforced by the AuthMiddleware (registered in app.py) — this route
itself does no additional auth check.

The "created" timestamp is a fixed Unix epoch marking the wrapper's initial
release date (2024-04-25). It is intentionally static — not tied to process
start time — so the response is stable across restarts and reproducible in
tests. OpenAI's own /v1/models returns static creation dates per model.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["models"])

# Fixed Unix timestamp for the codex-cli model entry.
# 2024-04-25 00:00:00 UTC → chosen to predate first deployment.
# Never change this value: it would break idempotency in consumer caches.
_WRAPPER_RELEASE_TS: int = 1714000000


@router.get("/v1/models")
async def list_models() -> dict[str, object]:
    """Return the single logical model exposed by this gateway.

    Shape matches OpenAI's GET /v1/models response exactly so the Python and
    Node SDKs can iterate ``client.models.list()`` without special-casing.
    """
    return {
        "object": "list",
        "data": [
            {
                "id": "codex-cli",
                "object": "model",
                "created": _WRAPPER_RELEASE_TS,
                "owned_by": "codex-wrapper",
            }
        ],
    }
