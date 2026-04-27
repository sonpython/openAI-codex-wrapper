# Phase 07: Observability

## Context Links
- Brainstorm: ../reports/brainstorm-260427-1358-codex-openai-wrapper.md (§7 secret leak risk, §9 success metrics)
- Codex JSONL: research/researcher-01-codex-jsonl-schema.md (event types for metric labels)
- OpenAI taxonomy: research/researcher-02-openai-event-taxonomy.md
- Phase 00: phase-00-bootstrap.md (logging/metrics/tracing stubs)
- Phase 02: phase-02-codex-runner.md (subprocess spans)
- Phase 06: phase-06-rate-limit-multi-tier.md (rate-limit metrics)
- Project rules: ../../.claude/rules/development-rules.md

## Overview
- Priority: high
- Status: pending
- Effort: M
- Description: Wire end-to-end observability across gateway, codex runner, and Arq workers. Three pillars: structured JSON logs (with redaction), Prometheus metrics, OpenTelemetry traces. Ship Grafana dashboards as code. This phase makes the system debuggable in production and feeds the alerting rules in phase 10.

## Key Insights
- Phase 00 already wired structlog stub + `/metrics` mount + OTEL no-op fallback. This phase fills in the redaction processor, metric definitions, custom spans, dashboards.
- **Secret-leak risk is HIGH** (brainstorm §7): redaction processor MUST run BEFORE JSON renderer and MUST be unit-tested with adversarial payloads. CI grep gate (phase 10) is a backstop, not primary defense.
- `request_id` propagated three hops: gateway middleware → OTEL span → codex subprocess env (`CODEX_REQUEST_ID`). Lets us correlate gateway log → trace → codex stderr.
- `/metrics` exposed on a SEPARATE internal port (default `:9090`) — never via Caddy. Public exposure is an info-disclosure risk and a scrape-amplification DoS vector.
- Sampling: 100% on errors + 10% otherwise. Prevents trace-storage blowup at 100 RPS while preserving error fidelity.

## Requirements

### Functional
- Every log line is JSON, ≤ 1 line, includes required fields (table below).
- Redaction processor scrubs known secret patterns before any log is emitted.
- `request_id` (UUIDv4) generated in gateway middleware; injected into log context, OTEL span attribute, codex subprocess env.
- Prometheus metrics exposed on internal port via dedicated ASGI app.
- OTEL traces exported via OTLP to local otel-collector (already in compose from phase 00).
- Custom spans wrap: `codex.subprocess.run`, `codex.event.parse`, `git.clone`, `git.diff`, `rate_limit.check`, `auth.verify`.
- Grafana dashboards committed as JSON under `infra/grafana/`.

### Non-Functional
- Logging adds < 1ms p99 per request.
- Metrics endpoint scrape completes < 200ms with default cardinality budget (< 5k series).
- All Python files ≤ 200 LOC.
- Redaction unit test coverage 100% on known patterns.
- Sampling rate env-tunable without code change.

## Architecture

```
request → gateway middleware (request_id assign)
            │
            ├─ structlog ctx bind {request_id, api_key_id, user_id, route}
            ├─ OTEL span start (parent: trace-context header if present)
            ├─ Prom counter inc (http_requests_total{route,status,method})
            │
            ▼
         route handler
            │  context propagated via contextvars
            │
            ├─ codex.runner.spawn:
            │     env["CODEX_REQUEST_ID"] = ctx.request_id
            │     OTEL span "codex.subprocess.run" {request_id, codex.cmd, exit_code}
            │     Prom histogram observe (codex_subprocess_duration_seconds)
            │
            ├─ jsonl_parser:
            │     OTEL span "codex.event.parse" per event
            │     Prom counter inc (codex_event_total{type})
            │
            └─ response:
                  Prom histogram observe (http_request_duration_seconds)
                  structlog .info("request.completed", ...redacted)
                  OTEL span end (status code, duration)

emit pipeline:
  structlog processors:
    [add_log_level, add_logger_name, TimeStamper(iso),
     bind_contextvars, RedactProcessor, JSONRenderer]
                          ^^^^^^^^^^^^^^^^^
                          MUST run before renderer

prometheus:
  CollectorRegistry (single, module-global)
  → /metrics ASGI app on internal :9090 (NOT via Caddy)

otel:
  TracerProvider + BatchSpanProcessor + OTLPSpanExporter
  → otel-collector :4317
  → forwarded to Tempo / Jaeger (env-configurable in collector config)
  Sampler: ParentBased(TraceIdRatioBased(0.1)) + always-on for spans w/ status=ERROR
```

### Required log fields (all log lines)

| Field | Type | Source |
|---|---|---|
| `ts` | RFC3339 UTC | TimeStamper processor |
| `level` | str | structlog level |
| `event` | str | message |
| `service` | str | bound: `gateway` or `worker` |
| `request_id` | str | middleware |
| `api_key_id` | str? | auth middleware (after phase 1) |
| `user_id` | str? | auth middleware |
| `route` | str | FastAPI route template |
| `duration_ms` | int? | observability middleware (response phase) |
| `status_code` | int? | response phase |
| `codex_event_count` | int? | runner (when applicable) |
| `codex_exit_code` | int? | runner (when applicable) |

### Redaction patterns (regex, case-insensitive)

| Pattern | Replacement | Why |
|---|---|---|
| `(?i)authorization` (key) | `***REDACTED***` | header values |
| `bearer\s+\S+` | `bearer ***` | inline strings |
| `OPENAI_API_KEY[=:]\s*\S+` | `OPENAI_API_KEY=***` | env dumps |
| `CODEX_API_KEY[=:]\s*\S+` | `CODEX_API_KEY=***` | env dumps |
| `cwk_[A-Za-z0-9_-]{20,}` | `cwk_***` | our key prefix |
| `sk-[A-Za-z0-9]{20,}` | `sk-***` | OpenAI keys |
| `ghp_[A-Za-z0-9]{20,}` | `ghp_***` | GitHub PAT |
| `github_pat_[A-Za-z0-9_]{20,}` | `github_pat_***` | new GitHub PAT |
| key matches `(?i)(secret|password|token|api[-_]?key)` | value → `***REDACTED***` | nested dicts |

## Related Code Files

### To create
- `src/observability/metrics.py` (extend phase-00 stub; ≤ 180 LOC) — Prom registry + named instruments + decorators.
- `src/observability/tracing.py` (extend phase-00 stub; ≤ 180 LOC) — OTEL provider, sampler, instrumentation hooks, custom span helpers.
- `src/observability/logging.py` (extend phase-00; ≤ 180 LOC) — RedactProcessor with regex patterns + recursive scrub.
- `src/gateway/middleware/request_id.py` (≤ 80 LOC) — assign UUIDv4, bind to contextvars + OTEL span attr.
- `src/gateway/middleware/observability.py` (≤ 150 LOC) — combines metric emit + log emit on response.
- `infra/grafana/api-overview.json` — req/s, latency, error rate.
- `infra/grafana/codex-pipeline.json` — subprocess duration, event mix, queue depth.
- `infra/grafana/rate-limits.json` — rejections, top consumers.
- `infra/otel-collector-config.yaml` — receivers + exporters (Tempo/Jaeger via env).
- `tests/unit/test_redaction.py` — adversarial payload scrub test.
- `tests/unit/test_metrics.py` — counter/histogram registration smoke.
- `tests/unit/test_request_id_middleware.py` — header propagation, contextvar binding.

### To modify
- `src/gateway/app.py` — register `request_id` + `observability` middleware; mount `/metrics` on internal port via separate uvicorn worker (or `make_asgi_app()` on dedicated FastAPI sub-app bound to `:9090`).
- `src/codex/runner.py` (phase 02) — read `CODEX_REQUEST_ID` from contextvars and inject into subprocess env; emit codex_subprocess_* metrics; wrap in `codex.subprocess.run` span.
- `src/codex/jsonl_parser.py` (phase 02) — bump `codex_event_total{type}` per event.
- `src/workers/arq_worker.py` (phase 05) — emit arq_* metrics + custom spans for `git.clone`, `git.diff`.
- `src/gateway/middleware/auth.py` (phase 01) — bump `auth_rejections_total{reason}`; add `auth.verify` span.
- `src/gateway/middleware/rate_limit.py` (phase 06) — bump `rate_limit_*` metrics; `rate_limit.check` span.
- `docker-compose.yml` — open internal port `:9090` for metrics; otel-collector config volume mount.
- `src/settings.py` — add `METRICS_PORT`, `OTEL_SAMPLING_RATIO`, `OTEL_SERVICE_NAME` (already exists).
- `.env.example` — document new env vars.

### To delete
(none)

## Implementation Steps

1. **Redaction processor** — Implement `RedactProcessor` callable for structlog. Compile regex list once at import. On each event_dict, recursively scrub: keys matching secret-key regex → value `***REDACTED***`; values matching value-pattern regex → substitution. Cap recursion at depth 5 to avoid pathological payload DoS.
2. **Update logging.py processor chain** — Insert `RedactProcessor()` AFTER `bind_contextvars` and BEFORE `JSONRenderer`. Add unit test feeding hostile payload (auth header, env dump, nested dict).
3. **request_id middleware** — `BaseHTTPMiddleware`. Read `X-Request-ID` header if present + valid UUID; else generate `uuid4()`. `structlog.contextvars.bind_contextvars(request_id=...)`. Set OTEL span attr `request.id`. Set response header `X-Request-ID` (echo back). Test: header preserved if valid, generated otherwise.
4. **Prometheus metrics module** — Single `CollectorRegistry`. Define instruments listed below. Provide decorator `@track_duration(histogram)` and helper `inc(counter, labels)`. Export `make_metrics_app()` returning ASGI app via `prometheus_client.make_asgi_app(registry=...)`.

   | Instrument | Type | Labels | Notes |
   |---|---|---|---|
   | `http_requests_total` | Counter | route, status, method | gateway middleware |
   | `http_request_duration_seconds` | Histogram | route | buckets 0.1, 0.25, 0.5, 1, 2, 5, 10, 30 |
   | `codex_subprocess_duration_seconds` | Histogram | exit_code_class | runner |
   | `codex_subprocess_exit_code_total` | Counter | code | runner |
   | `codex_event_total` | Counter | type | parser |
   | `codex_active_subprocess` | Gauge | — | runner inc/dec |
   | `arq_queue_depth` | Gauge | — | worker periodic |
   | `arq_jobs_active` | Gauge | — | worker |
   | `arq_job_duration_seconds` | Histogram | outcome | worker |
   | `arq_jobs_total` | Counter | status | worker |
   | `rate_limit_rejections_total` | Counter | dimension | rate_limit middleware |
   | `rate_limit_remaining` | Gauge | dimension, tier | rate_limit middleware |
   | `auth_rejections_total` | Counter | reason | auth middleware |
   | `db_query_duration_seconds` | Histogram | op | engine |
   | `db_pool_active` | Gauge | — | engine periodic |
   | `db_pool_idle` | Gauge | — | engine periodic |

5. **Internal metrics port** — In `app.py` lifespan, spawn metrics ASGI app on a SEPARATE uvicorn task bound to `0.0.0.0:9090`. Document: Caddy MUST NOT proxy `/metrics`. Compose: only Prometheus container reaches `:9090` over Docker network.
6. **OTEL setup** — `init_tracing(settings)`:
   - If `OTEL_EXPORTER_OTLP_ENDPOINT` unset → install no-op tracer (fallback from phase 00).
   - Else → `TracerProvider(resource=Resource(service.name=...))`, `BatchSpanProcessor(OTLPSpanExporter(endpoint=...))`.
   - Sampler: `ParentBased(root=TraceIdRatioBased(settings.OTEL_SAMPLING_RATIO))` + always-on for ERROR spans (custom sampler combining ratio + status).
   - Auto-instrumentors: `FastAPIInstrumentor.instrument_app(app)`, `AsyncPGInstrumentor`, `RedisInstrumentor`, `HTTPXClientInstrumentor`, `ArqInstrumentor` (if available; else manual spans in worker).
7. **Custom span helpers** — `@traced("name", attrs={...})` decorator that opens span, records exception, sets status. Use across runner, parser, auth, rate_limit, worker.
8. **request_id → codex env** — In `codex/runner.py`, before `asyncio.create_subprocess_exec`, copy current contextvars `request_id` into env dict as `CODEX_REQUEST_ID`. Even though codex itself doesn't honor it, it ends up in our subprocess wrapper logs and stderr-tail logs, allowing cross-correlation.
9. **observability middleware** — Last middleware in chain. On response: compute `duration_ms = (time.monotonic() - start) * 1000`; bump `http_requests_total` + `http_request_duration_seconds`; emit `request.completed` log line with redacted `route`, `status_code`, `duration_ms`. On 5xx: also emit `request.failed` at error level.
10. **otel-collector config** — `infra/otel-collector-config.yaml`:
    ```yaml
    receivers:
      otlp:
        protocols:
          grpc: { endpoint: 0.0.0.0:4317 }
    processors:
      batch: {}
      memory_limiter: { check_interval: 1s, limit_mib: 200 }
    exporters:
      otlphttp/tempo: { endpoint: ${TEMPO_ENDPOINT} }
      logging: { loglevel: warn }  # dev fallback
    service:
      pipelines:
        traces: { receivers: [otlp], processors: [memory_limiter, batch], exporters: [otlphttp/tempo, logging] }
    ```
11. **Grafana dashboards** — JSON exported from a local Grafana scratch instance. Three boards:

    | Dashboard | Panels |
    |---|---|
    | API Overview | RPS by route+status, p50/p95/p99 latency, error rate (5xx/total), active jobs gauge, top 10 routes by RPS |
    | Codex Pipeline | subprocess duration heatmap, event-type histogram (stacked), exit code mix (pie), queue depth, active subprocess gauge, codex_session_healthy (from phase 08) |
    | Rate Limits | rejections by dimension (RPM/TPM/concurrent/monthly), top api_keys by RPM/TPM consumption (table from `rate_limit_remaining`) |

12. **Settings additions** — `METRICS_PORT=9090`, `OTEL_SAMPLING_RATIO=0.1` (float 0–1), `LOG_LEVEL=INFO` (existing).
13. **Tests**:
    - `test_redaction.py`: feed payloads from §Redaction patterns table; assert each is scrubbed in resulting JSON line.
    - `test_metrics.py`: import module, assert all 16 instruments registered; bump each, scrape via `generate_latest()`, assert lines present.
    - `test_request_id_middleware.py`: TestClient request without header → response has `X-Request-ID` UUID; with valid header → echoed; with invalid → fresh UUID.
    - `test_observability_middleware.py`: assert `http_requests_total` and `http_request_duration_seconds` incremented after request; assert log line has required fields.

## Todo List
- [ ] RedactProcessor implemented + 100% pattern coverage in tests
- [ ] structlog processor chain updated (redaction before render)
- [ ] request_id middleware + contextvar bind + OTEL attr
- [ ] All 16 Prometheus instruments defined and exported
- [ ] Metrics ASGI app on internal port `:9090` (compose-internal only)
- [ ] OTEL tracer with parent-based + ratio sampler + always-on for errors
- [ ] Auto-instrumentors wired (FastAPI, asyncpg, redis, httpx)
- [ ] Custom spans on codex runner, parser, git ops, rate_limit, auth
- [ ] `CODEX_REQUEST_ID` env injection into subprocess
- [ ] observability middleware emits log + metrics on every response
- [ ] otel-collector-config.yaml committed
- [ ] 3 Grafana dashboards committed under `infra/grafana/`
- [ ] All unit tests pass; coverage on observability/* ≥ 90%
- [ ] No file > 200 LOC; ruff + mypy clean

## Success Criteria
- `curl :9090/metrics` returns Prom text with all 16 metric names present.
- `curl :8000/v1/models` (auth ok) → log line in stdout has `request_id`, `api_key_id`, `route=/v1/models`, `status_code=200`, `duration_ms`.
- Hostile payload `{"authorization":"Bearer sk-abc123","secret":"hunter2"}` logged → resulting JSON line contains NO substring `sk-abc123` and NO substring `hunter2`.
- `request_id` set by client header `X-Request-ID: <uuid>` → echoed back in response header AND propagates to codex subprocess env (verified in subprocess wrapper log).
- OTEL traces visible in otel-collector logs (logging exporter) for chat completion request: span tree `http.request → auth.verify → rate_limit.check → codex.subprocess.run → codex.event.parse (×N)`.
- Grafana dashboards load against local Prometheus scrape (smoke test in CI: `jq` valid).
- Sampling: at default 0.1, ~10% of successful requests sampled; 100% of 5xx sampled (verified by error injection test).

## Risk Assessment
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Redaction false-negative leaks secret | M | HIGH | Adversarial unit tests; CI grep gate (phase 10); periodic log-sample review |
| Metric cardinality explosion (e.g., user_id label) | M | M | NEVER label by user_id/api_key_id directly; aggregate per-tier or per-route only |
| `/metrics` accidentally exposed via Caddy | L | M | Caddyfile reverse_proxy explicit allow-list `/v1/*` only; integration test asserts 404 on `/metrics` from public path |
| OTEL export back-pressure stalls request path | L | M | BatchSpanProcessor + memory_limiter in collector; sampling caps volume |
| Sampler hides the one error you need | M | M | Always-on sampling for ERROR status overrides ratio |
| Log volume DoS to log-shipper | M | M | Rate-limit log lines per-request (cap 50/req); JSON renderer compact mode |
| Recursion bomb in redaction | L | L | Depth cap = 5; payload size cap (already enforced upstream by FastAPI body limit) |

## Security Considerations
- `/metrics` endpoint UNREACHABLE via public reverse proxy. Internal port only.
- `request_id` is opaque UUIDv4 — never embed user identifiers.
- Redaction is defense-in-depth, not the only layer: never log full request bodies; log only schema-validated fields.
- OTEL span attributes redacted via the same processor (manual span attr setters wrapped).
- otel-collector receives in-cluster only (no public ingress).

## Next Steps
- Phase 08 builds on metrics here for alerting (codex_session_unhealthy, queue_depth_high).
- Phase 10 deploys Prometheus + Grafana + Loki + Alertmanager and ingests these dashboards.
- Phase 09 tests assert `X-Request-ID` round-trip via OpenAI SDK custom headers.
