"""Live conformance, generated tests, contract-ci with git, and the MCP protocol."""

import json
import os
import subprocess
import sys
from pathlib import Path

import anyio
from mcp.shared.memory import create_connected_server_and_client_session

from k11_contract_qa import __version__, conformance, testgen
from k11_contract_qa.cli import main as cli
from k11_contract_qa.server import create_server
from k11_contract_qa.spec import load_spec


def test_live_conformance_catches_the_bug(project, service):
    spec = load_spec(project, "openapi.yaml")
    bad = conformance.check(spec, service.url, "GET /orders/{id}", {"id": "1001"})
    assert bad["verdict"] == "violates"
    assert {(v["rule"], v["path"]) for v in bad["violations"]} == {("schema_type", "total")}
    assert conformance.check(spec, service.url, "GET /orders")["verdict"] == "conforms"
    assert conformance.check(spec, service.url, "GET /orders/{id}", {"id": "nope"})["documented_as"] == "404"
    created = conformance.check(spec, service.url, "POST /orders",
                                body={"customerId": "c-7", "items": [{"sku": "A", "qty": 1}], "channel": "web"})
    assert created["verdict"] == "conforms", created


def test_generated_pytest_suite_runs(project, service, tmp_path):
    spec = load_spec(project, "openapi.yaml")
    ops = [spec.find("GET /orders"), spec.find("GET /orders/{id}")]
    files = testgen.pytest_suite(spec, ops)
    for rel, text in files.items():
        p = tmp_path / "gen" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    test_file = next(p for p in files if p.endswith(".py"))
    env = {**os.environ, "API_BASE_URL": service.url}
    out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", test_file],
                         cwd=tmp_path / "gen", env=env, capture_output=True, text=True)
    # the generated id '1001'-style example is "string", so GET /orders/{id} gets 404; list passes
    assert "1 passed" in out.stdout and "1 failed" in out.stdout, out.stdout[-800:]
    java = testgen.rest_assured_suite(spec, ops, "com.acme.contract")
    src = next(t for p, t in java.items() if p.endswith(".java"))
    assert "package com.acme.contract;" in src and 'matchesJsonSchemaInClasspath("schemas/list_orders.json")' in src
    assert src.count("{") == src.count("}")


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True,
                   capture_output=True)


def test_contract_ci_against_git_base(project, monkeypatch, tmp_path):
    (project / "api.yaml").write_text((project / "openapi.v1.yaml").read_text())
    git(project, "init", "-q", "-b", "main")
    git(project, "add", "api.yaml", ".qa-mcp/consumers.yaml")
    git(project, "commit", "-q", "-m", "v1")
    (project / "api.yaml").write_text((project / "openapi.yaml").read_text())
    out_file = tmp_path / "gh_output"
    monkeypatch.setenv("QA_MCP_ROOT", str(project))
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    assert cli(["contract-ci", "--spec", "api.yaml", "--base", "HEAD"]) == 1
    outputs = dict(line.split("=", 1) for line in out_file.read_text().splitlines())
    assert outputs["verdict"] == "breaking" and outputs["consumers_affected"] == "3" and outputs["needs_human"] == "true"
    md = (project / ".qa-mcp/contract-report/contract-report.md").read_text("utf8")
    assert "Breaking for known consumers" in md and "billing-svc" in md and "Needs a person before merge" in md
    assert "reads 'customer.email'" in md
    assert cli(["contract-ci", "--spec", "api.yaml", "--base", "HEAD", "--fail-on", "never"]) == 0
    assert cli(["contract-ci", "--spec", "api.yaml", "--base-spec", "api.yaml"]) == 0  # identical → compatible


def test_mcp_tools_over_protocol(project, service, monkeypatch):
    monkeypatch.setenv("QA_MCP_ROOT", str(project))
    monkeypatch.setenv("ORDER_TOKEN", "Bearer s3cret")

    async def go():
        async with create_connected_server_and_client_session(create_server()._mcp_server) as client:
            info = await client.initialize()
            assert (info.serverInfo.name, info.serverInfo.version) == ("k11-api-contract-qa", __version__)
            tools = sorted(t.name for t in (await client.list_tools()).tools)
            assert tools == ["assess_impact", "check_live_conformance", "diff_contracts", "discover_consumer_usage",
                             "generate_api_tests", "list_operations", "write_test"]

            def j(r):
                assert not r.isError, r.content[0].text
                return json.loads(r.content[0].text)

            ops = j(await client.call_tool("list_operations", {"spec": "openapi.yaml"}))
            assert len(ops["operations"]) == 4
            impact = j(await client.call_tool("assess_impact", {"base_spec": "openapi.v1.yaml", "head_spec": "openapi.yaml",
                                                               "write_report": True}))
            assert impact["verdict"] == "breaking" and impact["report"]["markdown"].endswith("contract-report.md")
            live = j(await client.call_tool("check_live_conformance", {
                "spec": "openapi.yaml", "base_url": service.url, "endpoint": "GET /orders/{id}",
                "path_params": {"id": "1001"}, "headers": {"Authorization": "${ENV:ORDER_TOKEN}"}}))
            assert live["verdict"] == "violates" and "s3cret" not in json.dumps(live)
            found = j(await client.call_tool("discover_consumer_usage", {"consumer_dir": "consumers/billing-svc", "spec": "openapi.yaml"}))
            assert found["endpoints"] == ["GET /orders/{id}"] and "OrderClient.java" in found["evidence"][0]["file"]
            gen = j(await client.call_tool("generate_api_tests", {"spec": "openapi.yaml", "endpoints": ["GET /orders"]}))
            name = next(p for p in gen["files"] if p.endswith(".py"))
            w = j(await client.call_tool("write_test", {"path": name, "content": gen["files"][name]}))
            assert w["written"] == name
            assert (await client.call_tool("write_test", {"path": "../evil.py", "content": "x"})).isError
            assert (await client.call_tool("write_test", {"path": "tests/x.sh", "content": "x"})).isError
            assert (await client.call_tool("diff_contracts", {"base_spec": "../../etc.yaml", "head_spec": "openapi.yaml"})).isError

    anyio.run(go)
