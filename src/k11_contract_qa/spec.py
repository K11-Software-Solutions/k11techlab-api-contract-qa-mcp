"""Load OpenAPI 3.x specs (JSON or YAML) and resolve local $refs.

Specs come from a file inside the project, or from git (``<ref>:<path>``, e.g.
``origin/main:openapi.yaml``) so CI can compare a pull request with its base.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
GIT_REF = re.compile(r"^([\w./~^@{}-]+):(.+)$")


class SpecError(ValueError):
    pass


def inside(root: Path, rel: str) -> Path:
    root = Path(root).resolve()
    full = (root / rel).resolve()
    if full != root and root not in full.parents:
        raise SpecError(f"{rel} must be inside the project folder")
    return full


def read_spec_text(root: Path, source: str) -> tuple[str, str]:
    """(text, label). ``source`` is a project path or ``<git-ref>:<path>``."""
    m = GIT_REF.match(source)
    if m and not Path(root, source).exists() and not re.match(r"^[A-Za-z]:[\\/]", source):
        ref, rel = m.groups()
        inside(root, rel)
        if ref.startswith("-"):
            raise SpecError("Invalid git ref")
        proc = subprocess.run(["git", "show", f"{ref}:{rel.replace(chr(92), '/')}"], cwd=root,
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise SpecError(f"Could not read {rel} at {ref}: {proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else 'git error'}")
        return proc.stdout, source
    path = inside(root, source)
    if not path.is_file():
        raise SpecError(f"Spec not found: {source}")
    return path.read_text("utf8"), source


def parse_spec(text: str, label: str = "spec") -> dict:
    try:
        data = json.loads(text) if text.lstrip().startswith("{") else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError) as e:
        raise SpecError(f"{label} is not valid JSON or YAML: {str(e).splitlines()[0]}") from None
    if not isinstance(data, dict) or not str(data.get("openapi", "")).startswith("3"):
        raise SpecError(f"{label} is not an OpenAPI 3.x document (missing 'openapi: 3.x')")
    return data


def resolve(spec: dict, node: Any, seen: frozenset = frozenset(), depth: int = 0) -> Any:
    """Inline local $refs (#/components/...). Recursive refs stop at the second visit."""
    if depth > 40:
        return {}
    if isinstance(node, dict):
        if "$ref" in node and isinstance(node["$ref"], str):
            ref = node["$ref"]
            if not ref.startswith("#/"):
                return {"x-unresolved-ref": ref}
            if ref in seen:
                return {"x-recursive-ref": ref.split("/")[-1]}
            target: Any = spec
            for part in ref[2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                target = target.get(part, {}) if isinstance(target, dict) else {}
            out = resolve(spec, copy.deepcopy(target), seen | {ref}, depth + 1)
            if isinstance(out, dict):
                out = {**out, "x-ref-name": ref.split("/")[-1]}
            return out
        return {k: resolve(spec, v, seen, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [resolve(spec, v, seen, depth + 1) for v in node]
    return node


def norm_path(path: str) -> str:
    """/users/{userId} and /users/{id} are the same endpoint."""
    return re.sub(r"\{[^}]+\}", "{}", path.rstrip("/") or "/")


@dataclass
class Operation:
    method: str
    path: str
    summary: str = ""
    operation_id: str = ""
    parameters: list[dict] = field(default_factory=list)
    request_body: dict | None = None
    responses: dict = field(default_factory=dict)
    security: list | None = None
    deprecated: bool = False
    meta: dict = field(default_factory=dict)  # format-specific: streaming, soap_action, ...

    @property
    def key(self) -> str:
        return f"{self.method.upper()} {self.path}"

    @property
    def norm_key(self) -> str:
        return f"{self.method.upper()} {norm_path(self.path)}"


FORMATS = ("openapi", "swagger", "graphql", "protobuf", "asyncapi", "wsdl")


@dataclass
class Spec:
    raw: dict
    label: str
    operations: dict[str, Operation]  # norm_key -> Operation
    format: str = "openapi"
    strict_inputs: bool = False  # unknown/removed inputs are rejected (GraphQL, SOAP)
    info: dict = field(default_factory=dict)

    @property
    def title(self) -> str:
        return str(self.info.get("title") or self.raw.get("info", {}).get("title", "API"))

    @property
    def version(self) -> str:
        return str(self.info.get("version") or self.raw.get("info", {}).get("version", ""))

    @property
    def http(self) -> bool:
        return self.format in ("openapi", "swagger")

    def find(self, endpoint: str) -> Operation | None:
        method, _, path = endpoint.strip().partition(" ")
        return self.operations.get(f"{method.upper()} {norm_path(path.strip())}")


def load_spec(root: Path, source: str) -> Spec:
    """Load any supported contract: OpenAPI 3.x, Swagger 2.0, GraphQL SDL or introspection JSON,
    protobuf (.proto, with imports), AsyncAPI 2.x/3.x, or WSDL 1.1."""
    from .formats import load_text

    text, label = read_spec_text(root, source)

    def related(name: str) -> str:
        """Read a file next to the main one (protobuf imports), from disk or the same git ref."""
        m = GIT_REF.match(source)
        if m and not Path(root, source).exists() and not re.match(r"^[A-Za-z]:[\\/]", source):
            ref, rel = m.groups()
            base = rel.replace("\\", "/").rsplit("/", 1)[0] if "/" in rel.replace("\\", "/") else ""
            return read_spec_text(root, f"{ref}:{base + '/' if base else ''}{name}")[0]
        base = Path(source).parent
        return read_spec_text(root, str(base / name))[0]

    return load_text(text, label, related)


def build_spec(raw: dict, label: str = "spec") -> Spec:
    ops: dict[str, Operation] = {}
    global_security = raw.get("security")
    for path, item in (raw.get("paths") or {}).items():
        item = resolve(raw, item) if isinstance(item, dict) else {}
        shared = item.get("parameters", [])
        for method in METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            params = {(p.get("in"), p.get("name")): p for p in shared if isinstance(p, dict)}
            params.update({(p.get("in"), p.get("name")): p for p in op.get("parameters", []) if isinstance(p, dict)})
            o = Operation(method=method, path=path, summary=op.get("summary", ""),
                          operation_id=op.get("operationId", ""), parameters=list(params.values()),
                          request_body=op.get("requestBody"), responses=op.get("responses") or {},
                          security=op.get("security", global_security), deprecated=bool(op.get("deprecated")))
            ops[o.norm_key] = o
    return Spec(raw, label, ops, "openapi")


def make_op(method: str, path: str, request: dict | None = None, response: dict | None = None,
            summary: str = "", meta: dict | None = None, deprecated: bool = False, request_required: bool = True) -> Operation:
    """An operation for a non-HTTP format, using the same request/response shape as OpenAPI."""
    rb = {"required": request_required, "content": {"application/json": {"schema": request}}} if request is not None else None
    resp = {"200": {"description": "ok", "content": {"application/json": {"schema": response}}}} if response is not None \
        else {"200": {"description": "ok"}}
    return Operation(method=method, path=path, summary=summary, request_body=rb, responses=resp,
                     meta=meta or {}, deprecated=deprecated)


def json_schema_of(content: dict | None) -> dict | None:
    """Schema of the JSON media type in a requestBody/response ``content`` map."""
    if not isinstance(content, dict):
        return None
    for media, body in content.items():
        if "json" in media and isinstance(body, dict):
            return body.get("schema") or {}
    for media, body in content.items():  # form posts (Swagger formData) and XML bodies follow the same rules
        if ("form" in media or "xml" in media) and isinstance(body, dict):
            return body.get("schema") or {}
    return None


def success_response(op: Operation) -> tuple[str, dict] | None:
    for code in sorted(op.responses, key=str):
        if str(code).startswith("2"):
            return str(code), op.responses[code] or {}
    return None
