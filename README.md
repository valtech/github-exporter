# github-exporter

GitHub Enterprise seat/billing metrics exporter for Prometheus.

## Exported feature coverage and API endpoints

The exporter collects aggregate enterprise billing/seat metrics and maps them to:

- `github_enterprise_license_seats_used{enterprise,feature}`
- `github_enterprise_license_seats_total{enterprise,feature}` (when available)
- `github_enterprise_license_seats_available{enterprise,feature}` (when available)
- `github_exporter_last_scrape_timestamp_seconds`

Runtime uses `githubkit` + `githubkit-schemas[ghec-2026-03-10]` typed clients and one canonical endpoint per supported enterprise feature:

- `ghec` (required):  
  `/enterprises/{enterprise}/consumed-licenses`
- `copilot` (required):  
  `/enterprises/{enterprise}/copilot/billing/seats?per_page=1`
- `advanced_security` (required):  
  `/enterprises/{enterprise}/settings/billing/advanced-security`

If a required feature endpoint is unavailable or unauthorized, the scrape fails immediately (HTTP 500 on `/metrics`).

## Required GitHub permissions/scopes

- Token identity must have enterprise-level access (typically enterprise owner or billing manager-equivalent access).
- Classic PAT: include at least `read:enterprise`.
- Fine-grained PAT or GitHub App token: grant equivalent **read access to enterprise billing/administration APIs** used above.

## Runtime configuration

All runtime options are available as CLI flags and environment variables:

- `--enterprise` / `GITHUB_ENTERPRISE` (required)
- `--token` / `GITHUB_TOKEN` (required)
- `--api-url` / `GITHUB_API_URL` (default: `https://api.github.com`)
- `--listen-address` / `EXPORTER_LISTEN_ADDRESS` (default: `0.0.0.0`)
- `--port` / `EXPORTER_PORT` (default: `9736`)
- `--timeout-seconds` / `EXPORTER_TIMEOUT_SECONDS` (default: `15`)
- `--once` / `EXPORTER_ONCE` (default: `false`)
- `--log-level` / `LOG_LEVEL` (default: `INFO`)

Run a single scrape to stdout:

```bash
EXPORTER_ONCE=true GITHUB_ENTERPRISE=<enterprise> GITHUB_TOKEN=<token> uv run github-exporter
```

Run HTTP exporter endpoint for Prometheus:

```bash
GITHUB_ENTERPRISE=<enterprise> GITHUB_TOKEN=<token> uv run github-exporter
```

HTTP endpoints exposed by this exporter:

- `GET /metrics` - Prometheus metrics
- `GET /healthz` - health response `ok`
- `GET /` - health response `ok`

## Privacy-conscious `gh` CLI checks (for Copilot-assisted validation)

When testing with `gh` in Copilot workflows, return only minimal fields so personal data is not unnecessarily returned to the LLM context.

Examples (aggregate values only):

```bash
export ENTERPRISE=<enterprise>
gh api "/enterprises/$ENTERPRISE/consumed-licenses" \
  --jq '{consumed:.total_seats_consumed,total:(.total_seats // .total_seats_purchased)}'
```

```bash
gh api "/enterprises/$ENTERPRISE/copilot/billing/seats?per_page=1" \
  --jq '{used:(.total_seats // .active_this_cycle // .total_active_users // .total_active_seats // .seats_in_use), seats_returned:(.seats|length)}'
```

If a verification query returns records, explicitly cap and narrow fields:

```bash
gh api graphql -f query='
query($enterprise:String!) {
  enterprise(slug:$enterprise) {
    organizations(first: 1) { totalCount nodes { login } }
  }
}' -F enterprise="$ENTERPRISE"
```

## Production query/transfer limits

- Exporter behavior already minimizes transfer by querying summary billing endpoints (not paginated user lists).
- For manual troubleshooting, prefer minimal projection (`--jq`) and strict limits (`first: 1`, `per_page=1`) where applicable.
- Use sensible Prometheus scrape intervals for billing metrics (typically minutes, not seconds).

## Docker

The included `Dockerfile` follows uv's Docker guidance with dependency-layer caching and uses a **versioned uv image tag pinned by SHA256**.  
See the `Dockerfile` for the current pinned reference.

Build:

```bash
docker build -t github-exporter:local .
```

Run:

```bash
docker run --rm \
  -e GITHUB_ENTERPRISE=<enterprise> \
  -e GITHUB_TOKEN=<token> \
  github-exporter:local
```

## Development

Install dependencies:

```bash
uv sync --locked
```

Run lint checks and formatting:

```bash
uv run ruff check .
uv run ruff format .
```

Run type checking:

```bash
uv run ty check
```
