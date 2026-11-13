"""Live conformance: call a running endpoint and check it against the spec.

Checks the status code is documented, the content type matches, and the JSON body
validates against the response schema (OpenAPI 3.0 schemas are converted to JSON
Schema: nullable, OAS-only keywords). Header values may use ${ENV:NAME} so tokens
never appear in tool arguments or results.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any
from urllib.parse import quote, urljoin

import httpx
from jsonschema import Draft202012Validator

from .spec import Operation, Spec

ENV_RE = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}")
OAS_ONLY = {"nullable", "discriminator", "readOnly", "writeOnly", "xml", "externalDocs", "example", "deprecated"}


def resolve_env(value: str) -> str:
    def sub(m: re.Match) -> str:
        if m.group(1) not in os.environ:
            raise ValueError(f"Environment variable {m.group(1)} is not set")
        return os.environ[m.group(1)]
    return ENV_RE.sub(sub, value)


def to_json_schema(schema: Any) -> Any:
    if isinstance(schema, list):
        return [to_json_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: to_json_schema(v) for k, v in schema.items() if k not in OAS_ONLY and not k.startswith("x-")}
    if schema.get("nullable"):
        t = out.get("type")
        if isinstance(t, str):
            out["type"] = [t, "null"]
        elif "enum" in out:
            out["enum"] = list(out["enum"]) + [None]
        else:
            out = {"anyOf": [out, {"type": "null"}]}
    return out


def _documented(op: Operation, status: int) -> tuple[str | None, dict]:
    codes = {str(c): r for c, r in op.responses.items()}
    for key in (str(status), f"{str(status)[0]}XX", f"{str(status)[0]}xx", "default"):
        if key in codes:
            return key, codes[key] or {}
    return None, {}


def check(spec: Spec, base_url: str, endpoint: str, path_params: dict | None = None, query: dict | None = None,
          body: Any = None, headers: dict | None = None, timeout_s: float = 15) -> dict[str, Any]:
    op = spec.find(endpoint)
    if not op:
        raise ValueError(f"{endpoint} is not in {spec.label}. Use list_operations to see the endpoints.")
    path = op.path
    for name, value in (path_params or {}).items():
        path = path.replace("{" + name + "}", quote(str(value), safe=""))
    if "{" in path:
        missing = re.findall(r"\{([^}]+)\}", path)
        raise ValueError(f"Missing path parameter(s): {', '.join(missing)}")
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    hdrs = {k: resolve_env(str(v)) for k, v in (headers or {}).items()}
    started = time.perf_counter()
    try:
        r = httpx.request(op.method.upper(), url, params=query or None, json=body, headers=hdrs,
                          timeout=timeout_s, follow_redirects=False)
    except httpx.HTTPError as e:
        return {"endpoint": op.key, "url": url, "verdict": "unreachable", "error": str(e)[:200]}
    elapsed = round((time.perf_counter() - started) * 1000)
    violations: list[dict] = []
    code_key, resp_spec = _documented(op, r.status_code)
    if code_key is None:
        violations.append({"rule": "undocumented_status", "path": "", "message":
                           f"Status {r.status_code} is not documented (documented: {sorted(map(str, op.responses))})"})
    ctype = r.headers.get("content-type", "").split(";")[0].strip()
    content = (resp_spec or {}).get("content") or {}
    if content and ctype and not any(ctype == m or (m.endswith("/*") and ctype.startswith(m[:-1])) for m in content):
        violations.append({"rule": "content_type", "path": "", "message":
                           f"Content-Type {ctype} not in documented {sorted(content)}"})
    schema = None
    for media, mt in content.items():
        if "json" in media and isinstance(mt, dict):
            schema = mt.get("schema")
    body_json: Any = None
    if schema is not None and "json" in ctype:
        try:
            body_json = r.json()
        except ValueError:
            violations.append({"rule": "invalid_json", "path": "", "message": "Body is not valid JSON"})
        else:
            v = Draft202012Validator(to_json_schema(schema))
            for err in sorted(v.iter_errors(body_json), key=lambda e: list(e.absolute_path))[:25]:
                p = "".join(f"[{x}]" if isinstance(x, int) else f".{x}" for x in err.absolute_path).lstrip(".")
                violations.append({"rule": f"schema_{err.validator}", "path": p or "(body)", "message": err.message[:200]})
    return {
        "endpoint": op.key, "url": url, "status": r.status_code, "documented_as": code_key,
        "content_type": ctype, "elapsed_ms": elapsed,
        "verdict": "conforms" if not violations else "violates",
        "violations": violations,
    }
