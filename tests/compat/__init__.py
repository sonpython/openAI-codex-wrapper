"""
Compatibility test suite for codex-wrapper.

These tests require the docker-compose.test.yml stack to be running.
They are NOT collected during ``pytest tests/unit`` runs — only when
the compat suite is invoked explicitly:

    docker compose -f docker-compose.test.yml run --rm test-runner \\
        pytest tests/compat -v

See tests/compat/README.md for local setup instructions.
"""
