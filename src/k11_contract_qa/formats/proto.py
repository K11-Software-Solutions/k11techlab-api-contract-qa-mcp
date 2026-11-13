"""protobuf / gRPC: .proto files -> Spec.

A small parser for proto2/proto3 covering what affects compatibility: packages,
imports, messages (nested), enums, fields with numbers and labels, ``oneof``,
``map<k, v>``, ``reserved`` numbers, and services with (streaming) RPCs.

Each RPC is an operation ``RPC <package>.<Service>/<Method>``. Message fields
carry ``x-field-number`` so the rules can catch renumbering, renames and removed
numbers that are not reserved. Enums are compared by value name (the JSON mapping).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from ..spec import Spec, SpecError, make_op

SCALARS = {
    "double": ("number", "double"), "float": ("number", "float"),
    "int32": ("integer", "int32"), "sint32": ("integer", "int32"), "sfixed32": ("integer", "int32"),
    "uint32": ("integer", "uint32"), "fixed32": ("integer", "uint32"),
    "int64": ("integer", "int64"), "sint64": ("integer", "int64"), "sfixed64": ("integer", "int64"),
    "uint64": ("integer", "uint64"), "fixed64": ("integer", "uint64"),
    "bool": ("boolean", None), "string": ("string", None), "bytes": ("string", "byte"),
}
WELL_KNOWN = {"google.protobuf.Timestamp": ("string", "date-time"), "google.protobuf.Duration": ("string", "duration"),
              "google.protobuf.Empty": ("object", None), "google.protobuf.Any": ("object", None),
              "google.protobuf.Struct": ("object", None), "google.protobuf.StringValue": ("string", None),
              "google.protobuf.Int32Value": ("integer", "int32"), "google.protobuf.Int64Value": ("integer", "int64"),
              "google.protobuf.BoolValue": ("boolean", None), "google.protobuf.DoubleValue": ("number", "double")}
MAX_DEPTH = 8


@dataclass
class Field:
    name: str
    type: str
    number: int
    label: str = ""  # repeated | optional | required | map
    key_type: str = ""


@dataclass
class Message:
    name: str  # fully qualified, e.g. shop.v1.Order or shop.v1.Order.Item
    fields: list[Field] = field(default_factory=list)
    reserved: set[int] = field(default_factory=set)


@dataclass
class Rpc:
    service: str
    name: str
    request: str
    response: str
    client_stream: bool = False
    server_stream: bool = False
    deprecated: bool = False


@dataclass
class Model:
    package: str = ""
    syntax: str = "proto3"
    messages: dict[str, Message] = field(default_factory=dict)
    enums: dict[str, list[str]] = field(default_factory=dict)
    rpcs: list[Rpc] = field(default_factory=list)


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[A-Za-z_][\w.]*|\d+|[{}()\[\];=<>,]')


def parse(text: str, model: Model | None = None, related: Callable[[str], str] | None = None,
          seen: set[str] | None = None) -> Model:
    model = model or Model()
    seen = seen if seen is not None else set()
    toks = TOKEN.findall(_strip_comments(text))
    i = 0
    file_pkg = ""

    def expect(t):
        nonlocal i
        if i >= len(toks) or toks[i] != t:
            raise SpecError(f"protobuf parse error near '{' '.join(toks[max(0, i - 3):i + 3])}': expected '{t}'")
        i += 1

    def skip_statement():
        nonlocal i
        depth = 0
        while i < len(toks):
            t = toks[i]
            i += 1
            if t == "{":
                depth += 1
            elif t == "}":
                depth -= 1
                if depth <= 0:
                    return
            elif t == ";" and depth == 0:
                return

    def parse_enum(prefix: str):
        nonlocal i
        name = toks[i]
        i += 1
        expect("{")
        values = []
        while toks[i] != "}":
            if toks[i] in ("option", "reserved"):
                skip_statement()
                continue
            values.append(toks[i])
            skip_statement()
        i += 1
        model.enums[f"{prefix}{name}"] = values

    def parse_message(prefix: str):
        nonlocal i
        name = toks[i]
        i += 1
        full = f"{prefix}{name}"
        msg = Message(full)
        model.messages[full] = msg
        expect("{")
        while toks[i] != "}":
            t = toks[i]
            if t == "message":
                i += 1
                parse_message(full + ".")
            elif t == "enum":
                i += 1
                parse_enum(full + ".")
            elif t in ("option", "extensions", "extend"):
                skip_statement()
            elif t == "reserved":
                i += 1
                while toks[i] != ";":
                    if toks[i].isdigit():
                        lo = int(toks[i])
                        if i + 2 < len(toks) and toks[i + 1] == "to":
                            hi = toks[i + 2]
                            hi = 536870911 if hi == "max" else int(hi)
                            msg.reserved.update(range(lo, min(hi, lo + 10000) + 1))
                            i += 3
                            continue
                        msg.reserved.add(lo)
                    i += 1
                i += 1
            elif t == "oneof":
                i += 2  # oneof name
                expect("{")
                while toks[i] != "}":
                    if toks[i] == "option":
                        skip_statement()
                        continue
                    ftype, fname = toks[i], toks[i + 1]
                    i += 2
                    expect("=")
                    num = int(toks[i])
                    i += 1
                    skip_statement() if toks[i] != ";" else None
                    if toks[i - 1] != ";" and i < len(toks) and toks[i] == ";":
                        i += 1
                    msg.fields.append(Field(fname, ftype, num, "optional"))
                i += 1
            elif t == "map":
                i += 1
                expect("<")
                ktype = toks[i]
                i += 1
                expect(",")
                vtype = toks[i]
                i += 1
                expect(">")
                fname = toks[i]
                i += 1
                expect("=")
                num = int(toks[i])
                i += 1
                skip_statement()
                msg.fields.append(Field(fname, vtype, num, "map", ktype))
            else:
                label = ""
                if t in ("repeated", "optional", "required"):
                    label = t
                    i += 1
                ftype, fname = toks[i], toks[i + 1]
                i += 2
                expect("=")
                num = int(toks[i])
                i += 1
                skip_statement()
                msg.fields.append(Field(fname, ftype, num, label))
        i += 1

    def parse_service(prefix: str):
        nonlocal i
        sname = toks[i]
        i += 1
        expect("{")
        while toks[i] != "}":
            if toks[i] != "rpc":
                skip_statement()
                continue
            i += 1
            mname = toks[i]
            i += 1
            expect("(")
            cs = toks[i] == "stream"
            if cs:
                i += 1
            req = toks[i]
            i += 1
            expect(")")
            expect("returns")
            expect("(")
            ss = toks[i] == "stream"
            if ss:
                i += 1
            resp = toks[i]
            i += 1
            expect(")")
            deprecated = False
            if toks[i] == "{":
                j = i
                depth = 0
                while j < len(toks):
                    if toks[j] == "{":
                        depth += 1
                    elif toks[j] == "}":
                        depth -= 1
                        if depth == 0:
                            break
                    if toks[j] == "deprecated" and j + 2 < len(toks) and toks[j + 2] == "true":
                        deprecated = True
                    j += 1
                i = j + 1
                if i < len(toks) and toks[i] == ";":
                    i += 1
            else:
                expect(";")
            model.rpcs.append(Rpc(f"{prefix}{sname}", mname, _qualify(req, prefix), _qualify(resp, prefix), cs, ss, deprecated))
        i += 1

    while i < len(toks):
        t = toks[i]
        if t == "syntax":
            i += 2
            model.syntax = toks[i].strip("\"'")
            skip_statement()
        elif t == "package":
            file_pkg = toks[i + 1]
            model.package = model.package or file_pkg
            i += 2
            expect(";")
        elif t == "import":
            i += 1
            if toks[i] in ("public", "weak"):
                i += 1
            name = toks[i].strip("\"'")
            i += 1
            expect(";")
            if related and name not in seen and not name.startswith("google/protobuf/"):
                seen.add(name)
                try:
                    parse(related(name.split("/")[-1]) if "/" in name and not _exists(related, name) else related(name),
                          model, related, seen)
                except SpecError:
                    raise
                except Exception:  # noqa: BLE001 - an import we cannot read: its types stay unresolved
                    pass
        elif t == "message":
            i += 1
            parse_message(f"{file_pkg}." if file_pkg else "")
        elif t == "enum":
            i += 1
            parse_enum(f"{file_pkg}." if file_pkg else "")
        elif t == "service":
            i += 1
            parse_service(f"{file_pkg}." if file_pkg else "")
        else:
            skip_statement()
    return model


def _exists(related, name) -> bool:
    try:
        related(name)
        return True
    except Exception:  # noqa: BLE001
        return False


def _qualify(name: str, prefix: str) -> str:
    return name.lstrip(".") if name.startswith(".") or "." in name else f"{prefix}{name}"


def _lookup(model: Model, name: str, scope: str) -> tuple[str, str] | None:
    """Resolve a type name the way protoc does: innermost scope outwards. Returns (kind, full name)."""
    name = name.lstrip(".")
    parts = scope.split(".") if scope else []
    for k in range(len(parts), -1, -1):
        cand = ".".join(parts[:k] + [name]) if k else name
        if cand in model.messages:
            return "message", cand
        if cand in model.enums:
            return "enum", cand
    return None


def _schema(model: Model, type_name: str, scope: str, depth: int, seen: frozenset) -> dict:
    if type_name in SCALARS:
        t, f = SCALARS[type_name]
        return {"type": t, **({"format": f} if f else {})}
    if type_name.lstrip(".") in WELL_KNOWN:
        t, f = WELL_KNOWN[type_name.lstrip(".")]
        return {"type": t, **({"format": f} if f else {})}
    found = _lookup(model, type_name, scope)
    if not found:
        return {"type": "object", "x-unresolved-ref": type_name}
    kind, full = found
    if kind == "enum":
        return {"type": "string", "enum": model.enums[full], "x-ref-name": full}
    return message_schema(model, full, depth + 1, seen)


def message_schema(model: Model, full: str, depth: int = 0, seen: frozenset = frozenset()) -> dict:
    if full in seen or depth > MAX_DEPTH:
        return {"type": "object", "x-recursive-ref": full}
    msg = model.messages.get(full)
    if msg is None:
        return {"type": "object", "x-unresolved-ref": full}
    props, req = {}, []
    for f in msg.fields:
        inner = _schema(model, f.type, full, depth, seen | {full})
        if f.label == "repeated":
            s = {"type": "array", "items": inner}
        elif f.label == "map":
            s = {"type": "object", "additionalProperties": inner}
        else:
            s = inner
        s = {**s, "x-field-number": f.number}
        props[f.name] = s
        if f.label == "required":
            req.append(f.name)
    out = {"type": "object", "properties": props, "x-ref-name": full, "x-reserved-numbers": sorted(msg.reserved)}
    if req:
        out["required"] = req
    return out


def build(text: str, label: str, related: Callable[[str], str] | None = None) -> Spec:
    model = parse(text, related=related)
    if not model.rpcs and not model.messages:
        raise SpecError(f"{label}: no services or messages found")
    ops = {}
    for r in model.rpcs:
        op = make_op("RPC", f"{r.service}/{r.name}", request=message_schema(model, r.request),
                     response=message_schema(model, r.response), deprecated=r.deprecated,
                     meta={"client_streaming": r.client_stream, "server_streaming": r.server_stream,
                           "request_type": r.request.split(".")[-1], "response_type": r.response.split(".")[-1]})
        ops[op.norm_key] = op
    title = model.package or (model.rpcs[0].service if model.rpcs else "protobuf")
    return Spec({"package": model.package, "syntax": model.syntax}, label, ops, "protobuf",
                info={"title": title, "version": model.syntax})
