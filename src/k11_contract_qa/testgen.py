"""Generate API contract tests from a spec: pytest + httpx, or Java REST Assured.

Each test calls the endpoint, checks the documented success status and validates
the body against the response schema. Schemas are written next to the tests, so
the generated suite needs no dependency on this tool.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .conformance import to_json_schema
from .spec import Operation, Spec, json_schema_of, success_response


def _ident(op: Operation) -> str:
    base = op.operation_id or f"{op.method}_{op.path}"
    words = re.findall(r"[A-Za-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1_\2", base))
    return "_".join(w.lower() for w in words)[:60] or "endpoint"


def _example(schema: dict | None, depth: int = 0) -> Any:
    if not isinstance(schema, dict) or depth > 6:
        return None
    for key in ("example", "default"):
        if key in schema:
            return schema[key]
    if schema.get("enum"):
        return schema["enum"][0]
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        req = set(schema.get("required") or [])
        return {k: _example(v, depth + 1) for k, v in (schema.get("properties") or {}).items() if k in req or not req}
    if t == "array":
        return [_example(schema.get("items") or {}, depth + 1)]
    fmt = schema.get("format", "")
    return {"integer": 1, "number": 1.5, "boolean": True}.get(t, "2026-01-01" if fmt == "date" else
            "2026-01-01T00:00:00Z" if fmt == "date-time" else "a@example.com" if fmt == "email" else
            "00000000-0000-0000-0000-000000000000" if fmt == "uuid" else "string")


def _plan(op: Operation) -> dict:
    succ = success_response(op)
    code, resp = succ if succ else ("200", {})
    schema = json_schema_of((resp or {}).get("content"))
    req_schema = json_schema_of((op.request_body or {}).get("content"))
    path_params = {p["name"]: _example(p.get("schema"), 0) or "1" for p in op.parameters if p.get("in") == "path"}
    query = {p["name"]: _example(p.get("schema"), 0) for p in op.parameters if p.get("in") == "query" and p.get("required")}
    return {"code": int(code) if code.isdigit() else 200, "schema": to_json_schema(schema) if schema else None,
            "body": _example(req_schema) if req_schema else None, "path_params": path_params, "query": query}


def pytest_suite(spec: Spec, ops: list[Operation]) -> dict[str, str]:
    """Files to write: tests/test_<api>_contract.py and tests/schemas/*.json."""
    api = re.sub(r"[^a-z0-9]+", "_", spec.title.lower()).strip("_") or "api"
    files: dict[str, str] = {}
    L = [
        f'"""Contract tests for {spec.title} {spec.version}, generated from {spec.label} by k11-api-contract-qa-mcp."""',
        "import json", "import os", "from pathlib import Path", "", "import httpx", "import pytest",
        "from jsonschema import Draft202012Validator", "",
        'BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8080")',
        'HEADERS = {"Authorization": os.environ["API_TOKEN"]} if os.environ.get("API_TOKEN") else {}',
        'SCHEMAS = Path(__file__).parent / "schemas"', "", "",
        "def check_schema(body, name):",
        '    schema = json.loads((SCHEMAS / f"{name}.json").read_text())',
        "    errors = [f\"{'/'.join(map(str, e.absolute_path)) or '(body)'}: {e.message}\" for e in Draft202012Validator(schema).iter_errors(body)]",
        '    assert not errors, "Response does not match the contract:\\n" + "\\n".join(errors)', "",
    ]
    for op in ops:
        p = _plan(op)
        name = _ident(op)
        path = op.path
        for k, v in p["path_params"].items():
            path = path.replace("{" + k + "}", str(v))
        L += ["", f"def test_{name}():", f'    """{op.key}{" - " + op.summary if op.summary else ""}"""',
              f"    # TODO: replace example values with real test data",
              f"    r = httpx.request({op.method.upper()!r}, BASE_URL + {path!r}, headers=HEADERS,"
              + (f" params={p['query']!r}," if p["query"] else "")
              + (f" json={p['body']!r}," if p["body"] is not None else "") + " timeout=15)",
              f"    assert r.status_code == {p['code']}, r.text[:300]"]
        if p["schema"]:
            files[f"tests/schemas/{name}.json"] = json.dumps(p["schema"], indent=2) + "\n"
            L.append(f'    check_schema(r.json(), "{name}")')
    files[f"tests/test_{api}_contract.py"] = "\n".join(L) + "\n"
    return files


def rest_assured_suite(spec: Spec, ops: list[Operation], package: str = "contract") -> dict[str, str]:
    cls = "".join(w.capitalize() for w in re.findall(r"[A-Za-z0-9]+", spec.title))[:40] + "ContractTest"
    files: dict[str, str] = {}
    L = [f"package {package};", "",
         "import static io.restassured.RestAssured.given;",
         "import static io.restassured.module.jsv.JsonSchemaValidator.matchesJsonSchemaInClasspath;", "",
         "import io.restassured.RestAssured;", "import org.junit.jupiter.api.BeforeAll;", "import org.junit.jupiter.api.Test;", "",
         f"/** Contract tests for {spec.title} {spec.version}, generated by k11-api-contract-qa-mcp. */",
         f"class {cls} {{", "", "    @BeforeAll", "    static void setUp() {",
         '        RestAssured.baseURI = System.getenv().getOrDefault("API_BASE_URL", "http://localhost:8080");',
         "    }"]
    for op in ops:
        p = _plan(op)
        name = _ident(op)
        method = "".join(w.capitalize() if i else w for i, w in enumerate(name.split("_")))
        path = op.path
        for k, v in p["path_params"].items():
            path = path.replace("{" + k + "}", str(v))
        chain = ["        given()"]
        if p["body"] is not None:
            chain += ['            .contentType("application/json")',
                      "            .body(" + json.dumps(json.dumps(p["body"])) + ")"]
        for k, v in p["query"].items():
            chain.append(f'            .queryParam("{k}", {json.dumps(str(v))})')
        chain += ["        .when()", f'            .{op.method.lower()}("{path}")', "        .then()",
                  f"            .statusCode({p['code']})"]
        if p["schema"]:
            files[f"src/test/resources/schemas/{name}.json"] = json.dumps(p["schema"], indent=2) + "\n"
            chain.append(f'            .body(matchesJsonSchemaInClasspath("schemas/{name}.json"))')
        chain[-1] += ";"
        L += ["", "    @Test", f"    void {method}() {{", "        // TODO: replace example values with real test data"] + chain + ["    }"]
    L += ["}", ""]
    files[f"src/test/java/{package.replace('.', '/')}/{cls}.java"] = "\n".join(L)
    return files
