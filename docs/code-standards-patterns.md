# Code Standards: Async, DI, Middleware Patterns

**Project:** Codex CLI OpenAI-Compatible Wrapper  
**Language:** Python 3.12  
**Framework:** FastAPI + SQLAlchemy 2.0 + Arq

---

## Async Patterns & Concurrency

### SQLAlchemy 2.0 Async

**Pattern:** Use `async def` functions with `AsyncSession`, `Mapped[T]` for ORM models.

```python
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column, Session
from sqlalchemy import select

# Good — async ORM
async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    stmt = select(User).where(User.email == email)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()

# Avoid
def get_user_by_email(session: Session, email: str) -> User | None:
    # sync in async context = blocking event loop
    pass
```

### FastAPI Dependency Injection

**Pattern:** Use `Depends()` in route signatures; avoid custom session management in route bodies.

```python
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

# Good — DI via Depends
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with main_session() as session:
        yield session

@router.get("/users/{user_id}")
async def get_user(
    user_id: int,
    session: AsyncSession = Depends(get_session),
) -> UserResponse:
    user = await get_user_by_id(session, user_id)
    return UserResponse.model_validate(user)

# Avoid — manual session in route
@router.get("/users/{user_id}")
async def get_user(user_id: int):
    async with main_session() as session:  # avoid; use Depends instead
        pass
```

### Background Tasks (Fire-and-Forget)

**Pattern:** Use `_BG_TASKS` set with `add_done_callback(discard)` to prevent premature GC.

```python
import asyncio

_BG_TASKS = set()

async def publish_event_async(job_id: str, event: dict) -> None:
    """Fire-and-forget publish to Redis pub/sub."""
    task = asyncio.create_task(_publish_impl(job_id, event))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)

async def _publish_impl(job_id: str, event: dict) -> None:
    channel = f"job:{job_id}:events"
    await redis.publish(channel, json.dumps(event))
```

### Database Session Management (Non-DI Context)

**Pattern:** Use context manager `async with main_session() as session:`, never async generator `get_session()` for non-route code.

```python
from src.db.engine import main_session, bg_session

# Good — in event handler (not route)
async def handle_job_completion(job_id: str) -> None:
    async with main_session() as session:
        job = await crud.jobs.get(session, job_id)
        job.status = "completed"
        await session.commit()

# Background writes (audit log, best-effort)
async def log_audit_event(user_id: str, action: str) -> None:
    try:
        async with bg_session() as session:
            await crud.audit_log.create(session, user_id=user_id, action=action)
            await session.commit()
    except Exception as e:
        # bg_session has tight timeout; silently drop on contention
        logger.exception("Audit log write failed (acceptable)", exc_info=e)
```

---

## Dependency Injection & Composition

### FastAPI Depends Pattern

**Use for routes; inject services via FastAPI's DI system:**

```python
from fastapi import Depends, APIRouter
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()

async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with main_session() as session:
        yield session

@router.post("/jobs", response_model=JobResponse)
async def enqueue_job(
    request: JobRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> JobResponse:
    job = await crud.jobs.create(session, request=request, user_id=user.id)
    return JobResponse.model_validate(job)
```

### Raw Middleware (Not BaseHTTPMiddleware for Streaming)

**Pattern:** Use raw ASGI middleware, not `BaseHTTPMiddleware`, for rate-limit + timeout (streaming-safe).

```python
# Good — raw ASGI (handles streaming correctly)
class RateLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Rate-limit logic
        request_key = f"{scope['client'][0]}:{scope['path']}"
        if is_rate_limited(request_key):
            await send({"type": "http.response.start", "status": 429, "headers": [...]})
            return

        async def send_with_rate_limit_headers(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message["headers"].append((b"x-ratelimit-remaining", b"99"))
            await send(message)

        await self.app(scope, receive, send_with_rate_limit_headers)

# Avoid — BaseHTTPMiddleware with streaming
from starlette.middleware.base import BaseHTTPMiddleware

class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # BaseHTTPMiddleware buffers streaming responses; breaks SSE
        response = await call_next(request)
        return response
```

---

## Rate-Limit Middleware Integration

### Headers in Scope State

**Pattern:** Middleware writes headers to scope state; routes read and use.

```python
# Middleware writes headers to scope state
async def __call__(self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
    # ... rate-limit check ...
    scope["state"]["rate_limit_headers"] = [
        (b"x-ratelimit-remaining-requests", b"3599"),
        (b"x-ratelimit-remaining-tokens", b"89500"),
    ]
    await self.app(scope, receive, send)

# Route reads and uses
@router.post("/chat/completions")
async def chat_completions_stream(request: Request) -> StreamingResponse:
    headers = request.state.rate_limit_headers
    return StreamingResponse(stream_generator(), headers=headers, media_type="text/event-stream")
```

---

## Server-Sent Events (SSE) Pattern

### Keepalive Helper

**Pattern:** Use `sse_helpers.keepalive_wrap()` to emit `: keepalive\n\n` every 15s on silent streams.

```python
from src.gateway.sse_helpers import keepalive_wrap

async def stream_chat_completions(request: Request) -> StreamingResponse:
    async def generate():
        try:
            async for chunk in codex_runner.stream():
                yield f"data: {json.dumps(chunk)}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
    
    return StreamingResponse(
        keepalive_wrap(generate(), interval=15.0),
        media_type="text/event-stream",
        headers=rate_limit_headers,
    )
```

### SSE Headers

**Pattern:** Set headers in route via `StreamingResponse(headers=...)` constructor, not middleware.

```python
# Good — headers in StreamingResponse
return StreamingResponse(
    generator(),
    media_type="text/event-stream",
    headers=[
        ("Cache-Control", "no-cache"),
        ("Connection", "keep-alive"),
        ("X-Accel-Buffering", "no"),  # Nginx
        ("X-RateLimit-Remaining-Requests", "3599"),
    ],
)
```

---

## Workspace & Path Safety

### Path Validation (C6 Red-Team Fix)

**Pattern:** Use `os.path.realpath()` + `os.path.commonpath()` to prevent `../` escape.

```python
import os
from pathlib import Path

def validate_path_inside(requested_path: str, workspace_root: str) -> Path:
    """Ensure requested_path stays within workspace_root (no ../ escape)."""
    root_real = os.path.realpath(workspace_root)
    path_real = os.path.realpath(requested_path)
    
    # commonpath raises ValueError if paths are on different drives (Windows)
    try:
        common = os.path.commonpath([root_real, path_real])
    except ValueError as e:
        raise InvalidWorkspacePath(f"Path escape detected: {e}")
    
    if common != root_real:
        raise InvalidWorkspacePath(f"Path {path_real} escapes workspace {root_real}")
    
    return Path(path_real)

# Usage
workspace_root = f"/tmp/job-{job_id}"
safe_path = validate_path_inside("/workspace/src/main.py", workspace_root)
```

---

## Tool-Calling: Schema Preservation

### Nested JSON Schema Inlining

**CRITICAL:** When formatting tool schemas for prompt injection, preserve full JSON schema structure, NOT flattened `name(param: type)` format.

```python
# Good — full JSON schema inlined
tools_prompt = """
Available tools:
- execute_services: Call one or more Home Assistant services
  parameters: {"type":"object","properties":{"list":{"type":"array",...}},...}

INSTRUCTIONS:
- Arguments MUST conform EXACTLY to that tool's parameters schema
- For arrays of objects: EVERY item must include ALL required keys
"""

# Avoid — flattened format (hides nested required keys)
tools_prompt = """
Available tools:
- execute_services: (list: array)
"""
# ^ Codex sees "list is array" but misses that items need "domain", "service", etc.
```

See `src/chat/tool_calling.py::format_tools_prompt()` for implementation.

---

## Database Migrations

### Alembic Configuration

**Pattern:** psycopg3 (sync driver for migrations), `gen_random_uuid()` server defaults, `RESTRICT` FKs.

```python
# alembic/versions/20260427_0001_init.py
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', UUID(as_uuid=False), server_default=sa.func.gen_random_uuid()),
        sa.Column('email', sa.String(255), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    
    op.create_table(
        'api_keys',
        sa.Column('id', UUID(as_uuid=False), server_default=sa.func.gen_random_uuid()),
        sa.Column('user_id', UUID(as_uuid=False), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='RESTRICT'),
    )

def downgrade() -> None:
    op.drop_table('api_keys')
    op.drop_table('users')
```

---

**Last Updated:** 2026-04-29 (tool-calling schema requirements, oversized doc split)
