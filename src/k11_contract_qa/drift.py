"""Deployed-contract drift: does what a running service publishes match the spec file?

Fetches the live contract and diffs it against the spec (spec = what clients were
built against, deployed = what they actually get):
  GraphQL   POST an introspection query to the endpoint
  WSDL      GET the WSDL (e.g. http://host/orders?wsdl)
  OpenAPI / Swagger / AsyncAPI   GET the published document (e.g. /v3/api-docs)
gRPC needs server reflection and is not covered.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from .conformance import resolve_env
from .diff import diff_specs, summary
from .formats import load_text
from .spec import Spec


def fetch(spec: Spec, url: str, headers: dict | None = None, timeout_s: float = 20) -> Spec:
    hdrs = {k: resolve_env(str(v)) for k, v in (headers or {}).items()}
    if spec.format == "graphql":
        from graphql import get_introspection_query

        r = httpx.post(url, json={"query": get_introspection_query(descriptions=False)}, headers=hdrs, timeout=timeout_s)
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            raise ValueError(f"Introspection failed (is it disabled?): {data['errors'][0].get('message', '')[:200]}")
        return load_text(json.dumps(data), f"{url} (introspection)")
    if spec.format == "protobuf":
        raise ValueError("Live gRPC checks need server reflection, which is not supported yet. "
                         "Compare against the deployed .proto files with diff_contracts instead.")
    r = httpx.get(url, headers=hdrs, timeout=timeout_s, follow_redirects=True)
    r.raise_for_status()
    return load_text(r.text, url)


def check(spec: Spec, url: str, headers: dict | None = None) -> dict[str, Any]:
    deployed = fetch(spec, url, headers)
    changes = diff_specs(spec, deployed)
    s = summary(changes)
    return {
        "spec": spec.label, "deployed": url, "format": spec.format,
        "verdict": "matches" if not changes else ("incompatible" if s["breaking"] else "drift"),
        "summary": s,
        "note": ("The deployed contract matches the spec." if not changes else
                 "Breaking entries are places where clients built from the spec will fail against the deployed service."),
        "changes": [c.to_dict() for c in changes],
    }
