"""Load any supported contract format into the shared Spec model.

Detection uses the content, with the file extension as a hint:
  OpenAPI 3.x · Swagger 2.0 · AsyncAPI 2.x/3.x   (JSON or YAML)
  GraphQL SDL (.graphql/.gql) or an introspection result (JSON)
  protobuf (.proto, imports read from next to the file)
  WSDL 1.1 (XML)
"""

from __future__ import annotations

import json
import re
from typing import Callable

import yaml

from ..spec import Spec, SpecError, build_spec

Related = Callable[[str], str]


def detect(text: str, label: str = "") -> str:
    t = text.lstrip("﻿").lstrip()
    low = label.lower()
    if low.endswith(".proto") or re.search(r'^\s*syntax\s*=\s*"proto[23]"', t, re.M) or \
            (re.search(r"^\s*service\s+\w+\s*\{", t, re.M) and re.search(r"^\s*message\s+\w+\s*\{", t, re.M)):
        return "protobuf"
    if t.startswith("<"):
        return "wsdl"
    if low.endswith((".graphql", ".gql", ".graphqls", ".sdl")):
        return "graphql"
    data = _load_data(t)
    if isinstance(data, dict):
        if str(data.get("openapi", "")).startswith("3"):
            return "openapi"
        if str(data.get("swagger", "")).startswith("2"):
            return "swagger"
        if "asyncapi" in data:
            return "asyncapi"
        if "__schema" in data or "__schema" in (data.get("data") or {}):
            return "graphql"
        return "unknown"
    if re.search(r"^\s*(type|schema|extend|interface|input|enum|union|scalar|directive)\b", t, re.M):
        return "graphql"
    return "unknown"


def _load_data(t: str):
    try:
        return json.loads(t) if t.startswith(("{", "[")) else yaml.safe_load(t)
    except (ValueError, yaml.YAMLError):
        return None


def load_text(text: str, label: str = "spec", related: Related | None = None) -> Spec:
    fmt = detect(text, label)
    t = text.lstrip("﻿")
    if fmt == "openapi":
        return build_spec(_load_data(t.lstrip()), label)
    if fmt == "swagger":
        from .swagger2 import convert
        spec = build_spec(convert(_load_data(t.lstrip())), label)
        spec.format = "swagger"
        return spec
    if fmt == "asyncapi":
        from .asyncapi import build
        return build(_load_data(t.lstrip()), label)
    if fmt == "graphql":
        from .gql import build
        return build(t, label)
    if fmt == "protobuf":
        from .proto import build
        return build(t, label, related)
    if fmt == "wsdl":
        from .wsdl import build
        return build(t, label)
    raise SpecError(f"{label}: unrecognised contract. Supported: OpenAPI 3.x, Swagger 2.0, GraphQL SDL or "
                    "introspection JSON, protobuf (.proto), AsyncAPI 2.x/3.x, WSDL 1.1.")
