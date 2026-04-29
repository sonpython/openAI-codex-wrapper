# Code Standards & Codebase Structure

**Project:** Codex CLI OpenAI-Compatible Wrapper  
**Language:** Python 3.12  
**Framework:** FastAPI + SQLAlchemy 2.0 + Arq  
**Enforcement:** mypy (strict), ruff (linter + formatter), pytest (615 tests)

## Table of Contents

- **[File Organization & Size](#file-organization--size)** — Size limits, grandfathered exceptions
- **[Naming Conventions](#naming-conventions)** — Files, classes, functions, constants
- **[Type Hints & Static Analysis](#type-hints--static-analysis)** — mypy strict mode
- **[Error Handling & Exceptions](#error-handling--exceptions)** — Exception hierarchy, OpenAI-compatible errors
- **[Logging & Observability](#logging--observability)** — structlog only, secret redaction
- **[Testing Standards](#testing-standards)** — Coverage, pytest config
- **[Detailed Patterns](#detailed-patterns)** — See related docs for async, DI, middleware

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

## Detailed Patterns

For in-depth coverage of specific patterns, see:

| Pattern | Document |
|---------|----------|
| Async/await, SQLAlchemy AsyncSession, background tasks, DI | [Code Standards: Async, DI, Middleware Patterns](code-standards-patterns.md) |
| FastAPI Depends, raw ASGI middleware, streaming | [Code Standards: Async, DI, Middleware Patterns](code-standards-patterns.md) |
| Rate-limit headers in scope state, SSE keepalive | [Code Standards: Async, DI, Middleware Patterns](code-standards-patterns.md) |
| Path validation (C6 fix), database migrations | [Code Standards: Async, DI, Middleware Patterns](code-standards-patterns.md) |
| Tool-calling schema preservation (critical for HA EOC) | [Code Standards: Async, DI, Middleware Patterns](code-standards-patterns.md) |

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
7. **FastAPI Depends for DI** (not manual session management in routes)
8. **Path validation** (realpath + commonpath to prevent ../ escape)
9. **Test coverage ≥ 75%** (critical modules ≥ 85%)
10. **Pinned deps, reproducible build** (uv lock + venv; no global pip)
11. **Tool-calling schemas:** Preserve full JSON schema (not flattened) for nested tool parameters

---

**Last Updated:** 2026-04-29 (tool-calling schema rules, oversized doc split)
