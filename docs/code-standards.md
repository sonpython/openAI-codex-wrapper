# Code Standards & Codebase Structure

**Project:** Codex CLI OpenAI-Compatible Wrapper  
**Language:** Python 3.12  
**Framework:** FastAPI + SQLAlchemy 2.0 + Arq  
**Enforcement:** mypy (strict), ruff (linter + formatter), pytest (615 tests)

---

## File Organization & Size

### Size Limits

**Primary rule:** ≤ 200 LOC per Python file for optimal context management.

**Grandfathered exceptions** (existing > 200 LOC):
- `src/codex/runner.py` (225 LOC) — Single responsibility; runner logic too intertwined
- `src/responses/events_emitter.py` (270 LOC) — Event payload generation; 50+ event types
- `src/gateway/routes/jobs.py` (289 LOC) — Three related job endpoints (POST/GET/DELETE)
- `src/gateway/middleware/rate_limit.py` (384 LOC) — Multi-tier rate limit (RPM, TPM, concurrent, monthly)
- `src/gateway/rate_limit_token_estimator.py` (227 LOC) — Token estimation heuristics + tables

**Splitting strategy when new file exceeds 200 LOC:**
1. Identify functional boundaries (e.g., handlers, validators, formatters)
2. Extract into separate `*_helper.py` or `*_impl.py` files
3. Use composition (import helpers into main file) — avoid inheritance
4. Example: if `chat_handler.py` grows > 200 LOC, split:
   - `chat_handler.py` (orchestration)
   - `chat_prompt_builder.py` (message formatting)
   - `chat_stream_helper.py` (SSE streaming logic)

---

## Naming Conventions

### Python Files & Modules

**Pattern:** `snake_case` with descriptive long names (PEP 8)

```python
# Good (self-documenting for grep/glob)
src/gateway/middleware/rate_limit_token_estimator.py
src/workers/repo_url_head_check.py
src/observability/alert_webhooks.py
src/codex/stderr_archive.py

# Avoid
src/gateway/ratelimit.py  (ambiguous)
src/workers/check.py      (too generic)
```

### Classes

**Pattern:** `PascalCase` (PEP 8)

```python
# Good
class CodexRunner:
    async def run(self) -> Generator[CodexEvent, None, None]:
        pass

class RateLimitMiddleware(BaseHTTPMiddleware):
    pass

class ResponsesEmitter:
    def chunk(self, content: str) -> dict:
        pass

# Avoid
class codex_runner:  (not Python convention)
class RateLimitMiddleWare:  (inconsistent capitalization)
```

### Functions & Methods

**Pattern:** `snake_case` (PEP 8)

```python
# Good
async def extract_bearer_token(request: Request) -> str:
    pass

def validate_api_key(key: str) -> bool:
    pass

# Avoid
def ExtractBearerToken(request):  (not Python convention)
def validate_API_key(key):  (inconsistent casing)
```

### Constants

**Pattern:** `UPPER_SNAKE_CASE` (PEP 8)

```python
# Good
MAX_WORKSPACE_SIZE_MB = 1024
CODEX_TIMEOUT_SECONDS = 300
RATE_LIMIT_WINDOW_MINUTES = 1

# In Postgres/Redis
AUDIT_LOG_TABLE = "audit_log"
RATE_LIMIT_KEY_PREFIX = "rl:"
```

---

## Type Hints & Static Analysis

### Mypy Configuration

**Mode:** `strict` (all files must pass mypy strict checking)

```bash
mypy src/ --strict
```

**Rules:**
- All function signatures require explicit type hints (parameters + return type)
- All class attributes require type hints (use `Annotated` for metadata)
- No `Any` unless absolutely necessary (comment why)
- No implicit `Optional` (use `Optional[T]` explicitly)

### Type Hint Style

```python
from __future__ import annotations
from typing import Optional, Generator, AsyncGenerator
from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

# Good
class User:
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    api_keys: Mapped[list["ApiKey"]] = relationship("ApiKey", back_populates="user")

async def extract_bearer_token(request: Request) -> str:
    auth_header: str = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise InvalidAuthError("Missing bearer token")
    return auth_header[7:]

def generate_json_response(data: dict[str, Any]) -> str:
    return json.dumps(data)

# Avoid
def extract_bearer_token(request):  (missing type hints)
    pass

def generate_json_response(data):  (missing type hints)
    pass

def some_function(x: Any) -> Any:  (Any without justification)
    pass
```

### Avoid `from __future__ import annotations` in Test Files

**Exception:** Test files with FastAPI route fixtures may omit `from __future__ import annotations` if Pydantic model resolution requires runtime evaluation.

```python
# tests/conftest.py — FastAPI fixtures
# (No `from __future__ import annotations` to allow Pydantic introspection)

@pytest.fixture
def client():
    app = create_app()
    return TestClient(app)

# src/gateway/app.py — Production code
# (Always use `from __future__ import annotations`)
from __future__ import annotations
```

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

## Error Handling & Exceptions

### Exception Hierarchy

**Source:** `src/codex/exceptions.py`

```python
# Good — domain-specific exception hierarchy
class CodexError(Exception):
    """Base class for all Codex wrapper errors."""
    pass

class CodexTimeoutError(CodexError):
    """Codex subprocess exceeded timeout."""
    pass

class WorkspaceError(CodexError):
    """Workspace creation/cleanup failed."""
    pass

class InvalidWorkspacePath(CodexError):
    """Workspace path escapes sandbox (C6 red-team fix)."""
    pass

# Good — OpenAI-shaped HTTP errors
from fastapi import HTTPException

raise HTTPException(
    status_code=429,
    detail={"error": {"message": "Rate limit exceeded", "type": "rate_limit_error"}},
    headers={"Retry-After": f"{reset_in_seconds}"},
)
```

### Error Response Format

**Pattern:** OpenAI-compatible error envelope for HTTP responses.

```python
# Good — OpenAI-shaped
{
  "error": {
    "message": "Invalid API key provided",
    "type": "invalid_request_error",
    "param": "api_key",
    "code": 401
  }
}

# Avoid
{
  "status": "error",
  "message": "Invalid API key"
}
```

### Try-Catch Best Practices

```python
# Good — specific exception types
async def clone_repo(url: str) -> None:
    try:
        await subprocess_run(["git", "clone", url])
    except subprocess.TimeoutExpired:
        raise CodexTimeoutError(f"Clone timed out after 60s: {url}")
    except subprocess.CalledProcessError as e:
        raise WorkspaceError(f"Clone failed: {e.stderr.decode()}")

# Avoid — bare except or broad Exception
async def clone_repo(url: str) -> None:
    try:
        await subprocess_run(["git", "clone", url])
    except Exception:  # too broad
        pass
```

---

## Logging & Observability

### Structured Logging (structlog only)

**Rule:** Never use `print()`, `logging.*`, or f-string debugging. Always use structlog.

```python
from src.observability.logging import get_logger

logger = get_logger(__name__)

# Good — structured
logger.info("chat.completions.started", model="codex", user_id=user.id)
logger.error("codex.runner.timeout", timeout_s=300, job_id=job_id)

# Avoid
print(f"Started chat for user {user.id}")  # not observable
logging.info("chat started")  # uses stdlib logging, not structlog
logger.info(f"chat started for {user.id}")  # no structure
```

### Secret Redaction

**Automatic:** All API keys, tokens, auth cookies redacted by `RedactionProcessor` in structlog config.

```python
# config in src/observability/logging.py
REDACTED_FIELDS = {"api_key", "Authorization", "X-API-Key", "auth_json", ...}

# Before log: {"api_key": "sk-abc123..."}
# After: {"api_key": "[REDACTED]"}
```

**When adding new secrets fields:**

```python
# In src/observability/logging.py
REDACTED_FIELDS.add("your_new_secret_field")

# Verify in CI: grep -r "sk-" tests/ logs/  (should only find fixtures)
```

### Request ID Propagation

**Pattern:** RequestID middleware (outermost) generates unique ID; all logs include it automatically.

```python
# Automatic via middleware context
logger.info("database.query", query="SELECT * FROM users WHERE id = ?", user_id=123)
# Output: {"event": "database.query", "request_id": "req-abc-123", "query": "...", "user_id": 123}
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

### Raw Middleware (Not BaseHTTPMiddleware for streaming)

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

## Rate-Limit Model

### Four Dimensions

| Dimension | Window | Enforcement | Storage |
|-----------|--------|-------------|---------|
| RPM | Sliding minute | Lua script (Redis) | redis key with EXPIRE |
| TPM | Sliding minute | Lua script (Redis) | counter with refresh-on-slide |
| Concurrent | Real-time | PEXPIRE refresh | redis counter + TTL |
| Monthly | Calendar month | Postgres counter + cache | `usage_counter` table |

### Rate-Limit Headers

**Pattern:** OpenAI-compatible `X-RateLimit-*` headers.

```python
# Response headers
X-RateLimit-Limit-Requests: 3600              # RPM quota
X-RateLimit-Remaining-Requests: 3599          # RPM remaining
X-RateLimit-Reset-Requests: 2026-04-27T12:01:00Z
X-RateLimit-Limit-Tokens: 90000               # TPM quota
X-RateLimit-Remaining-Tokens: 89500           # TPM remaining
X-RateLimit-Reset-Tokens: 2026-04-27T12:01:00Z
```

### Middleware Integration

**Pattern:** Routes read `request.state.rate_limit_headers` and pass to `StreamingResponse(headers=...)`.

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

## Testing Standards

### Minimum Coverage

- **Overall:** ≥ 75% coverage on unit tests
- **Critical modules:** auth, rate-limit, codex.runner, db.crud, gateway routes — ≥ 85%

### Pytest Configuration

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
python_files = ["test_*.py"]
python_classes = ["Test*"]
python_functions = ["test_*"]
addopts = "--cov=src --cov-report=term-missing --strict-markers"
markers = [
    "slow: marks tests as slow (deselect with '-m \"not slow\"')",
    "integration: marks tests as integration (requires external services)",
]
```

### Test File Structure

```python
# Good — descriptive test names + fixtures
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

@pytest.fixture
async def authenticated_client(client: TestClient, api_key: str):
    """Fixture: client with valid Bearer token."""
    client.headers["Authorization"] = f"Bearer {api_key}"
    return client

@pytest.mark.asyncio
async def test_rate_limit_rpm_enforcement(authenticated_client: TestClient):
    """Test: RPM limit rejects 61st request in 60s window."""
    for i in range(60):
        response = authenticated_client.post("/v1/chat/completions", ...)
        assert response.status_code == 200
    
    # 61st request should be rejected
    response = authenticated_client.post("/v1/chat/completions", ...)
    assert response.status_code == 429
    assert "X-RateLimit-Remaining-Requests" in response.headers

# Avoid
def test_rate_limit():  # vague name
    pass

@pytest.mark.asyncio
async def test_rpm(client):  # missing context
    pass
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
            yield f"event: error\ndata: {json.dumps({"error": str(e)})}\n\n"
    
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

## Documentation & Comments

### Module Docstrings

```python
"""
Chat completions endpoint (sync + SSE streaming).

This module bridges OpenAI SDK chat.completions.create() calls to Codex CLI.
Handles both blocking (gather full response) and streaming (SSE chunks) modes.

Key classes:
  - ChatCompletionsRequest: Pydantic schema for /v1/chat/completions POST body
  - ChatCompletionsResponse: OpenAI-shaped response wrapper

See: src/codex/runner.py for subprocess execution logic
"""
```

### Function Docstrings

```python
async def chat_completions_sync(
    request: ChatCompletionsRequest,
    user: User,
    session: AsyncSession,
) -> ChatCompletionsResponse:
    """
    Handle sync (non-streaming) chat completions request.
    
    Args:
        request: OpenAI SDK request (messages, max_tokens, etc.)
        user: Authenticated user (from middleware)
        session: DB session (from FastAPI Depends)
    
    Returns:
        ChatCompletionsResponse: Full response with usage + choices[0].message
    
    Raises:
        CodexTimeoutError: If Codex subprocess exceeds 5-min timeout
        RateLimitError: If user exceeds TPM/RPM/concurrent limits
        InvalidRequestError: If request validation fails
    
    Note:
        Workspace is auto-cleaned up post-response.
    """
```

### Inline Comments (Use Sparingly)

```python
# Good — explains *why*, not what
# C6 red-team fix: prevent ../ path escape by comparing realpath + commonpath
if os.path.commonpath([root, path]) != root:
    raise InvalidWorkspacePath(...)

# Avoid — obvious what the code does
# Check if the path contains ".."
if ".." in path:  # poor; obvious from code
    raise InvalidWorkspacePath(...)
```

---

## Development Workflow

### Pre-Commit Checklist

```bash
# Format
ruff format src/ tests/

# Lint
ruff check src/ tests/ --fix

# Type check
mypy src/ --strict

# Tests
pytest tests/unit/ -v --cov=src

# Security scan
bandit -r src/ -ll
```

### Running Tests

```bash
# All unit tests
pytest tests/unit/ -v

# Specific module
pytest tests/unit/test_rate_limit*.py -v

# With coverage
pytest tests/unit/ --cov=src --cov-report=html
# Open htmlcov/index.html

# Slow tests only
pytest tests/unit/ -m slow

# Integration tests (requires Docker stack)
pytest tests/compat/ -v
```

---

## Version Pinning & Dependencies

### Pinned Versions

**Rule:** All production deps pinned to minor version (`>=X.Y, <X+1`); critical deps to patch (`==X.Y.Z`).

```toml
# pyproject.toml
[project.dependencies]
fastapi = ">=0.100.0, <1.0"
sqlalchemy = { version = ">=2.0.0, <3.0", extras = ["asyncio"] }
asyncpg = ">=0.28.0, <1.0"
redis = { version = ">=5.0.0, <6.0", extras = ["asyncio"] }

# Critical: exact pin
[build-system]
requires = ["pdm-backend==2406.1"]
```

### `pyproject.toml` & Lock

```bash
# Create lock
uv lock

# Sync (reproducible install)
uv sync

# No global pip install; always use uv
```

---

## Summary: Golden Rules

1. **≤ 200 LOC per file** (split when exceeded; exceptions documented)
2. **Type hints everywhere** (mypy strict; no Any without comment)
3. **structlog only** (no print/logging; secret redaction automatic)
4. **Async/await throughout** (never block event loop; use context managers)
5. **OpenAI-compatible errors** (error envelope format for HTTP responses)
6. **Raw ASGI middleware for streaming** (not BaseHTTPMiddleware)
7. **Fastapi Depends for DI** (not manual session management in routes)
8. **Path validation** (realpath + commonpath to prevent ../ escape)
9. **Test coverage ≥ 75%** (critical modules ≥ 85%)
10. **Pinned deps, reproducible build** (uv lock + venv; no global pip)

---

**Last Updated:** 2026-04-27
