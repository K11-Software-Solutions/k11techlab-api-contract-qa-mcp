"""Convert a Swagger 2.0 document to the OpenAPI 3.0 shape the rules work on.

Covers what changes meaning between the two: body/formData parameters become a
requestBody, response schemas move under ``content`` (using ``produces``),
``definitions`` become ``components/schemas`` (with $refs rewritten), and
securityDefinitions become securitySchemes.
"""

from __future__ import annotations

import copy
from typing import Any

from ..spec import METHODS


def _rewrite_refs(node: Any) -> Any:
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                v = (v.replace("#/definitions/", "#/components/schemas/")
                      .replace("#/parameters/", "#/components/parameters/")
                      .replace("#/responses/", "#/components/responses/"))
            out[k] = _rewrite_refs(v)
        return out
    if isinstance(node, list):
        return [_rewrite_refs(v) for v in node]
    return node


def _param(p: dict) -> dict:
    """Swagger 2 non-body parameter -> OpenAPI 3 parameter (type info moves into schema)."""
    if "$ref" in p:
        return p
    keys = ("type", "format", "items", "enum", "default", "minimum", "maximum", "pattern", "maxLength", "minLength")
    schema = {k: p[k] for k in keys if k in p}
    out = {k: v for k, v in p.items() if k not in keys and k != "collectionFormat"}
    out["schema"] = schema
    return out


def convert(doc: dict) -> dict:
    doc = _rewrite_refs(copy.deepcopy(doc))
    consumes = doc.get("consumes") or ["application/json"]
    produces = doc.get("produces") or ["application/json"]
    out: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": doc.get("info", {}),
        "paths": {},
        "components": {
            "schemas": doc.get("definitions", {}),
            "parameters": {k: _param(v) for k, v in (doc.get("parameters") or {}).items() if v.get("in") != "body"},
            "securitySchemes": {},
        },
    }
    if "security" in doc:
        out["security"] = doc["security"]
    for name, sd in (doc.get("securityDefinitions") or {}).items():
        if sd.get("type") == "basic":
            out["components"]["securitySchemes"][name] = {"type": "http", "scheme": "basic"}
        elif sd.get("type") == "apiKey":
            out["components"]["securitySchemes"][name] = {"type": "apiKey", "name": sd.get("name"), "in": sd.get("in")}
        else:
            out["components"]["securitySchemes"][name] = {"type": "oauth2", "flows": {}}
    base_path = (doc.get("basePath") or "").rstrip("/")
    for path, item in (doc.get("paths") or {}).items():
        new_item: dict[str, Any] = {}
        shared = item.get("parameters", [])
        for method in METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            params = list(shared) + list(op.get("parameters", []))
            body = next((p for p in params if p.get("in") == "body"), None)
            form = [p for p in params if p.get("in") == "formData"]
            new_op = {k: v for k, v in op.items() if k not in ("parameters", "responses", "consumes", "produces")}
            new_op["parameters"] = [_param(p) for p in params if p.get("in") not in ("body", "formData")]
            op_consumes = op.get("consumes") or consumes
            if body is not None:
                new_op["requestBody"] = {"required": bool(body.get("required")),
                                         "content": {m: {"schema": body.get("schema", {})} for m in op_consumes}}
            elif form:
                props = {p["name"]: _param(p)["schema"] for p in form}
                req = [p["name"] for p in form if p.get("required")]
                media = "multipart/form-data" if any(p.get("type") == "file" for p in form) else "application/x-www-form-urlencoded"
                new_op["requestBody"] = {"required": bool(req), "content": {media: {"schema": {
                    "type": "object", "properties": props, **({"required": req} if req else {})}}}}
            responses = {}
            for code, resp in (op.get("responses") or {}).items():
                r = {"description": resp.get("description", "")}
                if "schema" in resp:
                    r["content"] = {m: {"schema": resp["schema"]} for m in (op.get("produces") or produces)}
                if "$ref" in resp:
                    r = resp
                responses[str(code)] = r
            new_op["responses"] = responses
            new_item[method] = new_op
        out["paths"][f"{base_path}{path}" if base_path else path] = new_item
    return out
