# k11 API Contract QA — MCP server & Claude Code plugin

<!-- mcp-name: io.github.K11-Software-Solutions/api-contract-qa -->

**Know which services an API change breaks before you merge it, and why.**

A pull request renames a field or tightens a request body. Its own tests pass. Three other services break in production. Spec-diff tools can tell you a change is "breaking", but not *who* it breaks, so teams either ignore the warnings or block every change. This MCP server answers the real question:

- **Breaking-change detection** between two OpenAPI 3.x specs, including `$ref`s, nested fields and array items, with the rule each change broke.
- **Consumer-aware impact.** It knows which services call which endpoints, and which fields they read or send. A removed field only counts against the consumers that use it.
- **A verdict with evidence:** breaking, breaking but no known consumer uses it, or compatible. When 2 or more consumers break, or a critical one does, it says a person should decide before merge.
- **Live conformance checks** against a running service: status codes, content type and the JSON body validated against the schema.
- **Consumer discovery** that scans a consumer's source code (Python, Java, TypeScript, Go, …) for calls to your endpoints, with file and line.
- **Contract test generation** for pytest + httpx or Java REST Assured.
- **`contract-ci`**: a pull-request comment that lists every affected consumer and the reason.

Deterministic rules, no LLM calls, the same result every run. Works with **Claude Code** (as a plugin with `/check-api-change` and `/api-contract-tests`), **Claude Desktop** and any MCP client. Listed in the official [MCP Registry](https://registry.modelcontextprotocol.io) as `io.github.K11-Software-Solutions/api-contract-qa`.

---

## See it in a pull request

In the demo, Order Service v2 changes `total` from a number to a string, drops `customer.email`, adds a required `channel` to `POST /orders` and removes `DELETE /orders/{id}`. Four services consume it. `contract-ci` posted:

> ## API contract check: 🔴 Breaking for known consumers
>
> **Order Service**: `main` → this PR. 11 changes, **6 breaking** (worst: critical); 4 known consumers, **3 affected**.
>
> > **Needs a person before merge:** 3 consumers break (threshold 2); high-criticality consumers break: notification-svc, billing-svc, checkout-web.
>
> | Consumer | Owner | Criticality | Breaking change | Why it affects them |
> |---|---|---|---|---|
> | **billing-svc** | @team-billing | critical | `GET /orders/{id}`: Response field 'total' changed type from number to string | reads 'total' |
> | **checkout-web** | @team-web | high | `POST /orders`: New required request field 'channel' | calls POST /orders without 'channel' |
> | **notification-svc** | @team-notify | high | `GET /orders/{id}`: Response field 'customer.email' was removed | reads 'customer.email' |
>
> **Breaking, no known consumer:** `DELETE /orders/{orderId}` was removed, plus two response changes on `POST /orders` that checkout-web doesn't read.

analytics-svc calls `GET /orders` too, but it reads only `id` and `status`, so it isn't flagged.

## Tools

| Tool | What it does |
|---|---|
| `list_operations` | Lists a spec's endpoints. |
| `diff_contracts` | Classifies every change between two specs: rule, field path, before/after, severity. |
| `assess_impact` | Which consumers a change breaks and why; the verdict and whether a person should decide. Optionally writes the report. |
| `discover_consumer_usage` | Finds a consumer's calls to your API in its source code and drafts its `consumers.yaml` entry. |
| `check_live_conformance` | Calls a running endpoint and checks it against the spec. Header values can use `${ENV:NAME}`. |
| `generate_api_tests` | Contract tests (status + JSON schema) for pytest + httpx or REST Assured. |
| `write_test` | Writes generated files under `tests/` or `src/test/` only, never overwriting unless asked. |

Specs are files in the project or git refs: `origin/main:openapi.yaml`.

## Tell it who your consumers are

`.qa-mcp/consumers.yaml`:

```yaml
consumers:
  - name: billing-svc
    repo: acme/billing-svc
    owner: "@team-billing"
    criticality: critical          # critical | high | medium | low
    uses:
      - endpoint: GET /orders/{id}
        reads: [id, total, currency]   # leave out to mean "any field"
  - name: checkout-web
    owner: "@team-web"
    criticality: high
    uses:
      - endpoint: POST /orders
        sends: [customerId, items]
        reads: [id]
```

Field lists are what make the verdict precise. Without them, any breaking change on an endpoint counts against everyone who calls it. Start with `discover_consumer_usage` on each consumer's code, then add the fields.

## In CI

```bash
k11-api-contract-qa-mcp contract-ci --spec openapi.yaml --base origin/main
```

It compares the spec in the pull request with `origin/main`, writes `contract-report.md` (the PR comment) and `contract-report.json`, sets GitHub Actions outputs (`verdict`, `breaking`, `consumers_affected`, `needs_human`, `report`) and exits non-zero according to `--fail-on`:

| `--fail-on` | Fails when |
|---|---|
| `breaking` (default) | a known consumer breaks |
| `needs-human` | 2+ consumers or a critical/high one break |
| `any-breaking` | any breaking change, used or not |
| `never` | report only |

Copy [`docs/contract-check.yml`](docs/contract-check.yml) to `.github/workflows/` to comment on every pull request that touches the spec.

## Install

Requires Python 3.10+.

### Claude Code: plugin (recommended)

Needs [uv](https://docs.astral.sh/uv/):

```text
/plugin marketplace add K11-Software-Solutions/k11techlab-api-contract-qa-mcp
/plugin install api-contract-qa@k11-api-contract-qa
```

### Claude Code: MCP server only

```bash
claude mcp add api-contract-qa -- uvx k11-api-contract-qa-mcp
```

### Claude Desktop and other MCP clients

```json
{
  "mcpServers": {
    "api-contract-qa": {
      "command": "uvx",
      "args": ["k11-api-contract-qa-mcp"],
      "env": { "QA_MCP_ROOT": "/path/to/your/service" }
    }
  }
}
```

Or `pip install k11-api-contract-qa-mcp`.

## Try the demo

```bash
cd demo/order-service
k11-api-contract-qa-mcp contract-ci --spec openapi.yaml --base-spec openapi.v1.yaml
python server.py --port 8080    # v2 implementation with one deliberate contract bug
```

Then ask Claude to `check_live_conformance` on `GET /orders/{id}` with `id` 1001. It finds that `total` is still sent as a number.

## Rules

| Rule | Breaking? |
|---|---|
| Endpoint removed | yes (critical) |
| Response field removed, type changed, became nullable, no longer guaranteed | yes |
| Success response or JSON media type removed | yes |
| New required request field / parameter, field or parameter became required | yes |
| Request enum value removed, request field newly restricted to an enum | yes |
| New authentication requirement | yes (critical) |
| Required request field removed | yes |
| New endpoint, optional field or parameter; response enum value added | no (enum additions flagged for exhaustive switches) |
| Path parameter renamed, endpoint deprecated | no |

The rules extend the K11 research on [cross-repository contract testing and change impact analysis](https://doi.org/10.18535/ijecs.v15i07.5599) (IJECS, 2026), whose reference system is [k11techlab-microservice-qa-system](https://github.com/K11-Software-Solutions/k11techlab-microservice-qa-system). This server adds `$ref`, nested and array-item rules and field-level consumer matching.

## How it compares

| | Spec diff tools (e.g. oasdiff) | Consumer-driven contracts (e.g. Pact) | k11 API Contract QA |
|---|---|---|---|
| Knows who breaks | No | Yes | Yes, field-level |
| Setup per consumer | None | Contract tests in every consumer | One YAML entry; discovery drafts it |
| Explains why | Rule only | Failing interaction | Rule, field, and what the consumer reads or sends |
| Usable by an AI agent | No | No | Yes, over MCP |

## Safety

- Specs, consumer folders and reports must be inside the project folder; git refs are read with `git show`, never a shell.
- `write_test` writes only under `tests/` or `src/test/`, and won't overwrite without asking.
- Tokens for live checks are read from the environment (`${ENV:NAME}`) and never echoed.

## Development

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # bin/pip on macOS/Linux
pytest   # rules, consumer impact, discovery, live conformance, generated tests, contract-ci, MCP (20)
```

## Roadmap

- Transitive impact (a consumer that re-exposes the data to its own consumers)
- AsyncAPI and gRPC contracts
- Optional LLM review for "uncertain" cases, as in the research system

---

Built by [K11 Software Solutions](https://k11softwaresolutions.com). Part of the K11 Tech Lab agentic QA series, alongside [Playwright QA](https://github.com/K11-Software-Solutions/k11techlab-playwright-qa-mcp), [Selenium QA](https://github.com/K11-Software-Solutions/k11techlab-selenium-qa-mcp) and [LLM Assure](https://github.com/K11-Software-Solutions/k11techlab-llm-assurance-toolkit). MIT licensed.
