"""
Unit tests for JobCreateRequest schema validation.

Covers:
  - repo_url: valid GitHub HTTPS, SSH rejected, private/non-GitHub rejected,
    .git suffix variants, trailing slash
  - branch: valid names, invalid characters, length boundary
  - task: non-empty, max length enforcement
  - run_tests: True rejected with clear message
  - mode: valid literals
  - timeout_seconds: boundary values
"""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from src.gateway.schemas.jobs import JobCreateRequest  # noqa: E402

# ── repo_url validation ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/openai/codex",
        "https://github.com/openai/codex.git",
        "https://github.com/openai/codex/",
        "https://github.com/my-org/my-repo",
        "https://github.com/user123/repo_name",
        "https://github.com/a/b.git/",
        "https://github.com/A/B",
    ],
)
def test_valid_github_urls(url: str) -> None:
    req = JobCreateRequest(repo_url=url, task="do something")
    assert req.repo_url == url


@pytest.mark.parametrize(
    "url",
    [
        # SSH
        "git@github.com:openai/codex.git",
        # HTTP (not HTTPS)
        "http://github.com/openai/codex",
        # non-GitHub host
        "https://gitlab.com/openai/codex",
        "https://bitbucket.org/user/repo",
        # localhost SSRF
        "https://localhost/user/repo",
        "https://127.0.0.1/user/repo",
        # bare path
        "github.com/openai/codex",
        # missing repo
        "https://github.com/openai",
        # empty
        "",
        # extra path depth
        "https://github.com/openai/codex/tree/main",
    ],
)
def test_invalid_repo_urls_rejected(url: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        JobCreateRequest(repo_url=url, task="do something")
    errors = exc_info.value.errors()
    assert any("repo_url" in str(e.get("loc", "")) for e in errors)


# ── branch validation ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "feature/my-branch",
        "release-1.0",
        "v2.0.0",
        "HEAD",
        "a" * 200,  # max length
    ],
)
def test_valid_branch_names(branch: str) -> None:
    req = JobCreateRequest(
        repo_url="https://github.com/openai/codex",
        branch=branch,
        task="do something",
    )
    assert req.branch == branch


@pytest.mark.parametrize(
    "branch",
    [
        "",  # empty
        "branch with space",
        "branch!name",
        "branch@name",
        "a" * 201,  # exceeds 200 chars
    ],
)
def test_invalid_branch_names_rejected(branch: str) -> None:
    with pytest.raises(ValidationError):
        JobCreateRequest(
            repo_url="https://github.com/openai/codex",
            branch=branch,
            task="do something",
        )


# ── task validation ───────────────────────────────────────────────────────────


def test_task_empty_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        JobCreateRequest(
            repo_url="https://github.com/openai/codex",
            task="",
        )
    assert any("task" in str(e.get("loc", "")) for e in exc_info.value.errors())


def test_task_whitespace_only_rejected() -> None:
    with pytest.raises(ValidationError):
        JobCreateRequest(
            repo_url="https://github.com/openai/codex",
            task="   \n\t  ",
        )


def test_task_max_length_accepted() -> None:
    req = JobCreateRequest(
        repo_url="https://github.com/openai/codex",
        task="x" * 100_000,
    )
    assert len(req.task) == 100_000


def test_task_over_max_length_rejected() -> None:
    with pytest.raises(ValidationError):
        JobCreateRequest(
            repo_url="https://github.com/openai/codex",
            task="x" * 100_001,
        )


# ── run_tests rejection ───────────────────────────────────────────────────────


def test_run_tests_true_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        JobCreateRequest(
            repo_url="https://github.com/openai/codex",
            task="do something",
            run_tests=True,
        )
    errors = exc_info.value.errors()
    assert any("run_tests" in str(e.get("loc", "")) for e in errors)
    # Error message must mention v1.1
    messages = " ".join(str(e.get("msg", "")) for e in errors)
    assert "v1.1" in messages


def test_run_tests_false_accepted() -> None:
    req = JobCreateRequest(
        repo_url="https://github.com/openai/codex",
        task="do something",
        run_tests=False,
    )
    assert req.run_tests is False


# ── mode validation ───────────────────────────────────────────────────────────


def test_mode_read_only_default() -> None:
    req = JobCreateRequest(repo_url="https://github.com/openai/codex", task="t")
    assert req.mode == "read-only"


def test_mode_workspace_write_accepted() -> None:
    req = JobCreateRequest(
        repo_url="https://github.com/openai/codex",
        task="t",
        mode="workspace-write",
    )
    assert req.mode == "workspace-write"


def test_mode_invalid_rejected() -> None:
    with pytest.raises(ValidationError):
        JobCreateRequest(
            repo_url="https://github.com/openai/codex",
            task="t",
            mode="danger-full-access",  # type: ignore[arg-type]
        )


# ── timeout_seconds ───────────────────────────────────────────────────────────


def test_timeout_none_accepted() -> None:
    req = JobCreateRequest(repo_url="https://github.com/openai/codex", task="t")
    assert req.timeout_seconds is None


def test_timeout_valid_accepted() -> None:
    req = JobCreateRequest(
        repo_url="https://github.com/openai/codex",
        task="t",
        timeout_seconds=300,
    )
    assert req.timeout_seconds == 300


def test_timeout_max_accepted() -> None:
    req = JobCreateRequest(
        repo_url="https://github.com/openai/codex",
        task="t",
        timeout_seconds=3600,
    )
    assert req.timeout_seconds == 3600


def test_timeout_over_max_rejected() -> None:
    with pytest.raises(ValidationError):
        JobCreateRequest(
            repo_url="https://github.com/openai/codex",
            task="t",
            timeout_seconds=3601,
        )


def test_timeout_zero_rejected() -> None:
    with pytest.raises(ValidationError):
        JobCreateRequest(
            repo_url="https://github.com/openai/codex",
            task="t",
            timeout_seconds=0,
        )
