"""GraphQL: SDL or an introspection result -> Spec, and query documents -> exact field usage.

Each Query, Mutation and Subscription field is an operation (``QUERY order``,
``MUTATION createOrder``). Arguments are the request; the return type is the
response. Non-null maps to required; nullable output fields are marked nullable,
so non-null -> nullable is reported as breaking for readers. GraphQL rejects
unknown arguments, so the spec is ``strict_inputs``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from graphql import (GraphQLEnumType, GraphQLInputObjectType, GraphQLInterfaceType, GraphQLList, GraphQLNonNull,
                     GraphQLObjectType, GraphQLScalarType, GraphQLSchema, GraphQLUnionType, build_client_schema,
                     build_schema, parse, print_schema)
from graphql.error import GraphQLError
from graphql.language import FieldNode, FragmentDefinitionNode, FragmentSpreadNode, InlineFragmentNode, OperationDefinitionNode

from ..spec import Spec, SpecError, make_op

SCALARS = {"Int": ("integer", "int32"), "Float": ("number", None), "String": ("string", None),
           "Boolean": ("boolean", None), "ID": ("string", "ID")}
MAX_DEPTH = 6


def load_schema(text: str, label: str) -> GraphQLSchema:
    t = text.strip()
    try:
        if t.startswith("{"):
            data = json.loads(t)
            return build_client_schema(data.get("data", data))
        return build_schema(t)
    except (GraphQLError, TypeError, ValueError, KeyError) as e:
        raise SpecError(f"{label} is not a valid GraphQL schema: {str(e).splitlines()[0]}") from None


def _unwrap(t) -> tuple[Any, bool, bool]:
    """(named type, non_null, is_list) for the outer type."""
    non_null = isinstance(t, GraphQLNonNull)
    if non_null:
        t = t.of_type
    if isinstance(t, GraphQLList):
        return t, non_null, True
    return t, non_null, False


def type_schema(t, depth: int = 0, seen: frozenset = frozenset(), output: bool = True) -> dict:
    t, non_null, is_list = _unwrap(t)
    if is_list:
        s = {"type": "array", "items": type_schema(t.of_type, depth + 1, seen, output)}
    elif isinstance(t, GraphQLScalarType):
        typ, fmt = SCALARS.get(t.name, ("string", t.name))
        s = {"type": typ, **({"format": fmt} if fmt else {})}
    elif isinstance(t, GraphQLEnumType):
        s = {"type": "string", "enum": list(t.values), "x-ref-name": t.name}
    elif isinstance(t, (GraphQLObjectType, GraphQLInterfaceType, GraphQLInputObjectType)):
        if t.name in seen or depth > MAX_DEPTH:
            s = {"type": "object", "x-recursive-ref": t.name}
        else:
            props, req = {}, []
            for name, f in t.fields.items():
                props[name] = type_schema(f.type, depth + 1, seen | {t.name}, output)
                if isinstance(f.type, GraphQLNonNull) and not (not output and getattr(f, "default_value", None) not in (None,)):
                    req.append(name)
                if getattr(f, "deprecation_reason", None):
                    props[name]["x-deprecated"] = f.deprecation_reason
            s = {"type": "object", "properties": props, "x-ref-name": t.name, **({"required": req} if req else {})}
    elif isinstance(t, GraphQLUnionType):
        s = {"oneOf": [{"x-ref-name": m.name} for m in t.types], "x-ref-name": t.name}
    else:
        s = {}
    if output and not non_null:
        s["nullable"] = True
    return s


def build(text: str, label: str) -> Spec:
    schema = load_schema(text, label)
    ops = {}
    for kind, root in (("QUERY", schema.query_type), ("MUTATION", schema.mutation_type),
                       ("SUBSCRIPTION", schema.subscription_type)):
        if root is None:
            continue
        for name, field in root.fields.items():
            props, req = {}, []
            for aname, arg in field.args.items():
                props[aname] = type_schema(arg.type, 1, frozenset(), output=False)
                if isinstance(arg.type, GraphQLNonNull) and arg.default_value is None:
                    req.append(aname)
            request = {"type": "object", "properties": props, **({"required": req} if req else {})}
            op = make_op(kind, name, request=request, response=type_schema(field.type),
                         summary=(field.description or "").strip().splitlines()[0] if field.description else "",
                         deprecated=bool(field.deprecation_reason), request_required=bool(req))
            ops[op.norm_key] = op
    sdl = print_schema(schema)
    return Spec({"sdl": sdl}, label, ops, "graphql", strict_inputs=True,
                info={"title": _title(text, label), "version": ""})


def _title(text: str, label: str) -> str:
    m = re.search(r"#\s*title:\s*(.+)", text)
    return m.group(1).strip() if m else re.sub(r"\.(graphqls?|gql|sdl|json)$", "", label.split("/")[-1].split(":")[-1]) or "GraphQL API"


# ---- consumers: what does a query document actually read? ------------------------------------------

DOC_PATTERNS = [
    re.compile(r"gql\s*`([^`]*)`", re.S),                        # JS/TS gql`...`
    re.compile(r"graphql\s*\(\s*`([^`]*)`", re.S),               # graphql(`...`)
    re.compile(r'"""(.*?)"""', re.S),                             # Python / Java text blocks
    re.compile(r"'''(.*?)'''", re.S),
]
LOOKS_LIKE_DOC = re.compile(r"^\s*(query|mutation|subscription|fragment)\b|^\s*\{", re.S)


def documents_in(text: str, suffix: str) -> list[tuple[int, str]]:
    """(line, document) pairs found in a source file."""
    if suffix in (".graphql", ".gql"):
        return [(1, text)]
    out = []
    for rx in DOC_PATTERNS:
        for m in rx.finditer(text):
            body = m.group(1)
            if LOOKS_LIKE_DOC.search(body):
                out.append((text.count("\n", 0, m.start()) + 1, body))
    return out


def usage(doc_text: str) -> list[dict]:
    """[{endpoint, reads, sends}] for each root field selected in the document."""
    try:
        doc = parse(doc_text)
    except GraphQLError:
        return []
    frags = {d.name.value: d for d in doc.definitions if isinstance(d, FragmentDefinitionNode)}

    def paths(sel_set, prefix="", depth=0) -> list[str]:
        out: list[str] = []
        if sel_set is None or depth > 12:
            return out
        for sel in sel_set.selections:
            if isinstance(sel, FieldNode):
                if sel.name.value.startswith("__"):
                    continue
                p = f"{prefix}.{sel.name.value}" if prefix else sel.name.value
                out.append(p)
                out += paths(sel.selection_set, p, depth + 1)
            elif isinstance(sel, InlineFragmentNode):
                out += paths(sel.selection_set, prefix, depth + 1)
            elif isinstance(sel, FragmentSpreadNode) and sel.name.value in frags:
                out += paths(frags[sel.name.value].selection_set, prefix, depth + 1)
        return out

    uses = []
    for d in doc.definitions:
        if not isinstance(d, OperationDefinitionNode):
            continue
        kind = d.operation.value.upper()
        for sel in d.selection_set.selections:
            if isinstance(sel, FieldNode) and not sel.name.value.startswith("__"):
                uses.append({"endpoint": f"{kind} {sel.name.value}",
                             "reads": paths(sel.selection_set),
                             "sends": [a.name.value for a in sel.arguments]})
    return uses
