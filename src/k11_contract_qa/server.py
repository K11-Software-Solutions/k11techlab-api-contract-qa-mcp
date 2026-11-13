"""MCP server: API contract testing with consumer-aware impact and evidence."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal, Optional

from mcp.server.fastmcp import FastMCP

from . import __version__
from . import conformance, consumers, drift, report, testgen
from .diff import diff_specs, summary
from .spec import SpecError, inside, load_spec


def project_root() -> Path:
    return Path(os.environ.get("QA_MCP_ROOT") or os.getcwd()).resolve()


def _consumers_file(root: Path, rel: str | None) -> Path:
    return inside(root, rel or ".qa-mcp/consumers.yaml")


def create_server() -> FastMCP:
    mcp = FastMCP(
        "k11-api-contract-qa",
        instructions=(
            "API contract testing for OpenAPI 3.x, Swagger 2.0, GraphQL (SDL or introspection JSON), gRPC/protobuf "
            "(.proto), AsyncAPI 2.x/3.x and SOAP/WSDL 1.1; the format is detected automatically. Specs are project paths "
            "or git refs like origin/main:openapi.yaml. Operations are named 'GET /orders/{id}', 'QUERY order', "
            "'RPC pkg.Service/Method', 'EMITS order.created' / 'RECEIVES order.cancel', or 'SOAP Port/Operation'. "
            "Use diff_contracts / assess_impact before merging a change, check_deployed_contract to compare a running "
            "service with the spec, check_live_conformance for HTTP responses, and generate_api_tests for REST tests. "
            "Results are deterministic. Put tokens in headers as ${ENV:NAME}, never literally."
        ),
    )

    @mcp.tool(title="List operations")
    def list_operations(spec: str) -> dict:
        """List the operations in a contract (OpenAPI, Swagger, GraphQL, .proto, AsyncAPI or WSDL; a project path or
        <git-ref>:<path>). Also tells you the detected format."""
        s = load_spec(project_root(), spec)
        return {"api": s.title, "version": s.version, "format": s.format, "operations": [
            {"endpoint": o.key, "summary": o.summary, "operationId": o.operation_id, "deprecated": o.deprecated}
            for o in sorted(s.operations.values(), key=lambda o: (o.path, o.method))]}

    @mcp.tool(title="Diff contracts")
    def diff_contracts(base_spec: str, head_spec: str, breaking_only: bool = False) -> dict:
        """Compare two versions of a contract (any supported format) and classify every change as breaking or
        compatible, with the rule, field path, before/after and severity.
        Example: base_spec='origin/main:openapi.yaml', head_spec='openapi.yaml'."""
        root = project_root()
        changes = diff_specs(load_spec(root, base_spec), load_spec(root, head_spec))
        shown = [c for c in changes if c.breaking] if breaking_only else changes
        return {"summary": summary(changes), "changes": [c.to_dict() for c in shown]}

    @mcp.tool(title="Assess impact on consumers")
    def assess_impact(base_spec: str, head_spec: str, consumers_file: Optional[str] = None,
                      write_report: bool = False) -> dict:
        """Which known consumers a spec change breaks, and why (field-level when consumers list the fields they
        read/send). Verdict: breaking | breaking_no_known_consumers | compatible; needs_human when 2+ consumers or a
        high-criticality consumer break. Consumers come from .qa-mcp/consumers.yaml."""
        root = project_root()
        base, head = load_spec(root, base_spec), load_spec(root, head_spec)
        changes = diff_specs(base, head)
        cons = consumers.load_consumers(_consumers_file(root, consumers_file))
        impact = consumers.assess(changes, cons)
        out: dict[str, Any] = {"api": head.title, **impact}
        if write_report:
            r = report.build(base_spec, head_spec, head.title, changes, impact)
            md, js = report.write(r, root / ".qa-mcp" / "contract-report")
            out["report"] = {"markdown": md.relative_to(root).as_posix(), "json": js.relative_to(root).as_posix()}
        return out

    @mcp.tool(title="Discover consumer usage")
    def discover_consumer_usage(consumer_dir: str, spec: str, consumer_name: Optional[str] = None) -> dict:
        """Scan a consumer service's source code (inside the project) for calls to the provider, with file and line
        as evidence, and draft its consumers.yaml entry. HTTP paths, GraphQL query documents (exact fields read),
        gRPC stub calls, SOAP client calls, and AsyncAPI topic names."""
        root = project_root()
        d = inside(root, consumer_dir)
        if not d.is_dir():
            raise ValueError(f"Not a folder: {consumer_dir}")
        hits = consumers.discover_usage(d, load_spec(root, spec))
        name = consumer_name or d.name
        return {"consumer": name, "calls_found": len(hits), "endpoints": sorted({h["endpoint"] for h in hits}),
                "evidence": hits, "consumers_yaml": consumers.usage_to_yaml(name, hits),
                "note": "Add 'reads'/'sends' field lists to make impact checks field-precise."}

    @mcp.tool(title="Check live conformance")
    def check_live_conformance(spec: str, base_url: str, endpoint: str, path_params: Optional[dict] = None,
                               query: Optional[dict] = None, body: Optional[Any] = None,
                               headers: Optional[dict] = None) -> dict:
        """Call a running endpoint (e.g. endpoint='GET /orders/{id}', path_params={'id': '1001'}) and check status,
        content type and the JSON body against the spec's response schema. Header values may use ${ENV:NAME}."""
        if not re.match(r"^https?://", base_url):
            raise ValueError("base_url must start with http:// or https://")
        s = load_spec(project_root(), spec)
        if not s.http:
            raise ValueError(f"check_live_conformance is for OpenAPI/Swagger HTTP APIs; this is {s.format}. "
                             "Use check_deployed_contract to compare the running service with the spec.")
        return conformance.check(s, base_url, endpoint, path_params, query, body, headers)

    @mcp.tool(title="Check deployed contract")
    def check_deployed_contract(spec: str, url: str, headers: Optional[dict] = None) -> dict:
        """Compare what a running service publishes with the spec file: GraphQL introspection (url = the GraphQL
        endpoint), a WSDL URL (e.g. http://host/orders?wsdl), or a published OpenAPI/Swagger/AsyncAPI document
        (e.g. http://host/v3/api-docs). Verdict: matches | drift | incompatible. gRPC is not supported (needs
        reflection). Header values may use ${ENV:NAME}."""
        if not re.match(r"^https?://", url):
            raise ValueError("url must start with http:// or https://")
        return drift.check(load_spec(project_root(), spec), url, headers)

    @mcp.tool(title="Generate API tests")
    def generate_api_tests(spec: str, endpoints: Optional[list[str]] = None,
                           framework: Literal["pytest", "rest-assured"] = "pytest", java_package: str = "contract") -> dict:
        """Generate contract tests (status + JSON schema) for some or all endpoints: pytest + httpx, or Java REST
        Assured (JUnit 5). Returns the files; write them with write_test."""
        s = load_spec(project_root(), spec)
        if not s.http:
            raise ValueError(f"Test generation supports OpenAPI and Swagger HTTP APIs; this is {s.format}.")
        ops = [s.find(e) for e in endpoints] if endpoints else sorted(s.operations.values(), key=lambda o: (o.path, o.method))
        missing = [e for e, o in zip(endpoints or [], ops) if o is None]
        if missing:
            raise ValueError(f"Not in the spec: {', '.join(missing)}")
        if framework == "rest-assured" and not re.fullmatch(r"[a-z_]\w*(\.[a-z_]\w*)*", java_package):
            raise ValueError("java_package must look like com.example.contract")
        files = testgen.pytest_suite(s, ops) if framework == "pytest" else testgen.rest_assured_suite(s, ops, java_package)
        deps = ("pip install pytest httpx jsonschema" if framework == "pytest" else
                "Maven: io.rest-assured:rest-assured and io.rest-assured:json-schema-validator, JUnit 5")
        return {"framework": framework, "files": files, "dependencies": deps,
                "note": "Example values come from the spec; replace them with real test data."}

    @mcp.tool(title="Write test file")
    def write_test(path: str, content: str, overwrite: bool = False) -> dict:
        """Write a generated file under tests/ or src/test/ only. Won't overwrite unless asked."""
        root = project_root()
        rel = path.replace("\\", "/")
        if not rel.startswith(("tests/", "src/test/")):
            raise ValueError("Files can only be written under tests/ or src/test/")
        if not re.search(r"\.(py|java|json)$", rel):
            raise ValueError("Only .py, .java and .json files")
        target = inside(root, rel)
        if target.exists() and not overwrite:
            raise ValueError(f"{rel} exists. Pass overwrite=true to replace it.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, "utf8")
        return {"written": rel, "bytes": len(content.encode("utf8"))}

    mcp._mcp_server.version = __version__  # FastMCP (mcp 1.x) would report the SDK's version
    return mcp


def serve() -> None:
    create_server().run()


__all__ = ["create_server", "serve", "SpecError"]
