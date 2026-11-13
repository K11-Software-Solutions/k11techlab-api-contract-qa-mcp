"""Classify the differences between two contracts as breaking or compatible.

Works on the shared Spec model, so the same rules cover OpenAPI, Swagger 2.0,
GraphQL, protobuf, AsyncAPI and WSDL, with format-specific rules on top
(GraphQL/SOAP reject unknown inputs; protobuf field numbers and streaming).
Every change carries the rule it matched, the endpoint and field path,
before/after values and a severity, so a report can show exactly why.
Rules follow the K11 cross-repository contract testing research (IJECS 2026),
extended to $refs, nested objects and array items.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .spec import Operation, Spec, json_schema_of, success_response

SEVERITY = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}

# What inputs and outputs are called in each format, for readable descriptions.
NOUNS = {
    "openapi": ("request field", "response field", "endpoint"),
    "swagger": ("request field", "response field", "endpoint"),
    "graphql": ("argument/input field", "result field", "operation"),
    "protobuf": ("request message field", "response message field", "RPC"),
    "asyncapi": ("received message field", "emitted message field", "channel message"),
    "wsdl": ("request element", "response element", "SOAP operation"),
}
# Operation signature details where any difference breaks callers.
SIGNATURE_META = {"client_streaming": "client streaming", "server_streaming": "server streaming",
                  "soap_action": "SOAPAction", "soap_style": "SOAP binding style", "request_type": "request message type",
                  "response_type": "response message type"}
SIGNATURE_WIRE_SAFE = {"request_type", "response_type"}  # renaming a protobuf message type is wire-safe


@dataclass
class Change:
    rule: str
    breaking: bool
    severity: str
    endpoint: str
    location: str  # endpoint | parameter | request | response | security
    field: str = ""
    before: str = ""
    after: str = ""
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Ctx:
    fmt: str = "openapi"
    strict: bool = False  # removed/unknown inputs are rejected

    def noun(self, where: str) -> str:
        req, resp, _ = NOUNS.get(self.fmt, NOUNS["openapi"])
        return {"request": req, "response": resp, "parameter": "parameter"}.get(where, where)


def _type(schema: dict | None) -> str:
    if not isinstance(schema, dict) or not schema:
        return ""
    t = schema.get("type")
    if isinstance(t, list):
        t = "|".join(sorted(str(x) for x in t if x != "null"))
    if not t:
        if "properties" in schema:
            t = "object"
        elif "items" in schema:
            t = "array"
        elif any(k in schema for k in ("oneOf", "anyOf", "allOf")):
            return "composite"
        else:
            return ""
    fmt = schema.get("format")
    return f"{t}({fmt})" if fmt and t in ("string", "integer", "number") else str(t)


def _props(schema: dict) -> dict[str, dict]:
    props = dict(schema.get("properties") or {})
    for part in schema.get("allOf") or []:  # flatten allOf objects
        if isinstance(part, dict):
            props.update(part.get("properties") or {})
    return props


def _required(schema: dict) -> set[str]:
    req = set(schema.get("required") or [])
    for part in schema.get("allOf") or []:
        if isinstance(part, dict):
            req |= set(part.get("required") or [])
    return req


def _walk(ctx: Ctx, ep: str, where: str, base: dict, head: dict, path: str, out: list[Change], direction: str,
          depth: int = 0, skip_nullable: bool = False) -> None:
    """Compare two schemas. direction: 'request' (the caller writes it) or 'response' (the caller reads it)."""
    if depth > 12 or not isinstance(base, dict) or not isinstance(head, dict):
        return
    noun = ctx.noun(where)
    bt, ht = _type(base), _type(head)
    label = path or "(body)"
    if bt and ht and bt != ht and "composite" not in (bt, ht):
        out.append(Change("type_changed", True, "high", ep, where, label, bt, ht,
                          f"{noun.capitalize()} '{label}' changed type from {bt} to {ht}"))
        return
    b_enum, h_enum = base.get("enum"), head.get("enum")
    if isinstance(b_enum, list) and isinstance(h_enum, list):
        removed = [v for v in b_enum if v not in h_enum]
        added = [v for v in h_enum if v not in b_enum]
        if direction == "request" and removed:
            out.append(Change("enum_value_removed", True, "high", ep, where, label, str(b_enum), str(h_enum),
                              f"{noun.capitalize()} '{label}' no longer accepts {removed}"))
        elif direction == "response" and added:
            out.append(Change("enum_value_added", False, "low", ep, where, label, str(b_enum), str(h_enum),
                              f"{noun.capitalize()} '{label}' may now be {added}; check consumers that switch on it"))
        elif direction == "response" and removed:
            out.append(Change("enum_value_removed", False, "info", ep, where, label, str(b_enum), str(h_enum),
                              f"{noun.capitalize()} '{label}' is no longer {removed}"))
    elif direction == "request" and isinstance(h_enum, list) and b_enum is None:
        out.append(Change("enum_restricted", True, "medium", ep, where, label, "any", str(h_enum),
                          f"{noun.capitalize()} '{label}' is now restricted to {h_enum}"))
    if direction == "response" and head.get("nullable") and not base.get("nullable") and not skip_nullable:
        out.append(Change("became_nullable", True, "medium", ep, where, label, "non-null", "nullable",
                          f"{noun.capitalize()} '{label}' can now be null"))

    if "items" in base or "items" in head or "array" in (bt, ht):
        _walk(ctx, ep, where, base.get("items") or {}, head.get("items") or {}, f"{path}[]", out, direction, depth + 1)
        return

    bp, hp = _props(base), _props(head)
    if not bp and not hp:
        return
    breq, hreq = _required(base), _required(head)

    # protobuf: field numbers are the wire identity
    renamed: dict[str, str] = {}
    bnum = {n: s.get("x-field-number") for n, s in bp.items() if s.get("x-field-number") is not None}
    hnum = {s.get("x-field-number"): n for n, s in hp.items() if s.get("x-field-number") is not None}
    reserved = set(head.get("x-reserved-numbers") or [])
    for name, num in bnum.items():
        if name not in hp and num in hnum and hnum[num] not in bp:
            renamed[name] = hnum[num]
    for old, new in renamed.items():
        fpath = f"{path}.{old}" if path else old
        same_type = _type(bp[old]) == _type(hp[new])
        out.append(Change("field_renamed", True, "medium" if same_type else "critical", ep, where, fpath, old, new,
                          f"{noun.capitalize()} '{fpath}' renamed to '{new}' (field {bnum[old]})"
                          + ("; binary-compatible, but breaks JSON mapping and generated code" if same_type
                             else f"; type also changed from {_type(bp[old])} to {_type(hp[new])}, which breaks the wire format")))

    for name in sorted(set(bp) | set(hp)):
        if name in renamed or name in renamed.values():
            continue
        fpath = f"{path}.{name}" if path else name
        if name in bp and name not in hp:
            note = ""
            if name in bnum and bnum[name] not in reserved:
                note = f"; field number {bnum[name]} is not reserved, so it could be reused by mistake"
            if direction == "response":
                out.append(Change("field_removed", True, "high", ep, where, fpath, _type(bp[name]), "",
                                  f"{noun.capitalize()} '{fpath}' was removed{note}"))
            else:
                breaking = name in breq or ctx.strict
                why = (" (it was required)" if name in breq else
                       "; the contract rejects unknown inputs, so callers still sending it will fail" if ctx.strict else
                       "; callers still sending it may be rejected if the server forbids unknown fields")
                out.append(Change("field_removed", breaking, "high" if breaking else "low", ep, where, fpath,
                                  _type(bp[name]), "", f"{noun.capitalize()} '{fpath}' was removed{why}{note}"))
        elif name in hp and name not in bp:
            if direction == "request" and name in hreq:
                out.append(Change("required_field_added", True, "high", ep, where, fpath, "", _type(hp[name]),
                                  f"New required {noun} '{fpath}'"))
            else:
                out.append(Change("field_added", False, "low", ep, where, fpath, "", _type(hp[name]),
                                  f"New optional {noun} '{fpath}'"))
        else:
            b_num, h_num = bp[name].get("x-field-number"), hp[name].get("x-field-number")
            if b_num is not None and h_num is not None and b_num != h_num:
                out.append(Change("field_number_changed", True, "critical", ep, where, fpath, str(b_num), str(h_num),
                                  f"{noun.capitalize()} '{fpath}' moved from field {b_num} to {h_num}; existing clients "
                                  "will read and write the wrong field"))
            if direction == "request" and name in hreq and name not in breq:
                out.append(Change("field_became_required", True, "high", ep, where, fpath, "optional", "required",
                                  f"{noun.capitalize()} '{fpath}' became required"))
            no_longer = direction == "response" and name in breq and name not in hreq
            if no_longer:
                out.append(Change("field_no_longer_guaranteed", True, "medium", ep, where, fpath, "required",
                                  "optional", f"{noun.capitalize()} '{fpath}' is no longer guaranteed (it can now be missing or null)"))
            _walk(ctx, ep, where, bp[name], hp[name], fpath, out, direction, depth + 1, skip_nullable=no_longer)


def _walk_or_replaced(ctx: Ctx, ep: str, where: str, base: dict, head: dict, out: list[Change],
                      btype: str | None, htype: str | None) -> None:
    """When a message type was swapped for a different one, report one clear change instead of a
    field-by-field comparison of two unrelated messages."""
    found: list[Change] = []
    _walk(ctx, ep, where, base, head, "", found, where)
    if btype and htype and btype != htype and any(c.breaking for c in found):
        n = sum(c.breaking for c in found)
        out.append(Change("message_replaced", True, "high", ep, where, "(message)", btype, htype,
                          f"{'Request' if where == 'request' else 'Response'} message of {ep} replaced: {btype} → {htype} "
                          f"({n} incompatible field difference{'s' if n != 1 else ''})"))
        return
    out.extend(found)


def _param_map(op: Operation) -> dict[tuple[str, str], dict]:
    return {(p.get("in", ""), p.get("name", "")): p for p in op.parameters}


def _diff_op(ctx: Ctx, b: Operation, h: Operation, out: list[Change]) -> None:
    ep = h.key
    kind = NOUNS.get(ctx.fmt, NOUNS["openapi"])[2]
    if h.deprecated and not b.deprecated:
        out.append(Change("deprecated", False, "info", ep, "endpoint", description=f"{ep} is now deprecated"))
    # signature details (streaming, SOAPAction, binding style)
    for key, label in SIGNATURE_META.items():
        bv, hv = b.meta.get(key), h.meta.get(key)
        if bv != hv and not (bv is None and hv is None) and key in b.meta and key in h.meta:
            safe = key in SIGNATURE_WIRE_SAFE
            out.append(Change("signature_changed", not safe, "low" if safe else "high", ep, "endpoint", label,
                              str(bv), str(hv), f"{label[:1].upper() + label[1:]} of {ep} changed from {bv} to {hv}"
                              + ("; wire-compatible, but generated client code changes" if safe else "")))
    # parameters (path parameter names may be renamed freely)
    bpar, hpar = _param_map(b), _param_map(h)
    bpath = [p for (loc, _), p in bpar.items() if loc == "path"]
    for key in sorted(set(bpar) | set(hpar)):
        loc, name = key
        if loc == "path":
            continue
        label = f"{loc}:{name}"
        if key in bpar and key not in hpar:
            out.append(Change("parameter_removed", bool(bpar[key].get("required")), "medium" if bpar[key].get("required") else "low",
                              ep, "parameter", label, "present", "", f"Parameter '{name}' ({loc}) was removed"))
        elif key in hpar and key not in bpar:
            if hpar[key].get("required"):
                out.append(Change("required_parameter_added", True, "high", ep, "parameter", label, "", "required",
                                  f"New required parameter '{name}' ({loc})"))
            else:
                out.append(Change("parameter_added", False, "low", ep, "parameter", label, "", "optional",
                                  f"New optional parameter '{name}' ({loc})"))
        else:
            if hpar[key].get("required") and not bpar[key].get("required"):
                out.append(Change("parameter_became_required", True, "high", ep, "parameter", label, "optional",
                                  "required", f"Parameter '{name}' ({loc}) became required"))
            _walk(ctx, ep, "parameter", bpar[key].get("schema") or {}, hpar[key].get("schema") or {}, label, out, "request")
    hpath = [p for (loc, _), p in hpar.items() if loc == "path"]
    for bp_, hp_ in zip(bpath, hpath):
        _walk(ctx, ep, "parameter", bp_.get("schema") or {}, hp_.get("schema") or {}, f"path:{hp_.get('name')}", out, "request")

    # request body
    bbody, hbody = b.request_body or {}, h.request_body or {}
    if hbody.get("required") and not bbody.get("required") and bbody and ctx.fmt in ("openapi", "swagger"):
        out.append(Change("request_body_became_required", True, "high", ep, "request", "(body)", "optional", "required",
                          f"Request body of {ep} became required"))
    elif hbody and not bbody and hbody.get("required") and ctx.fmt in ("openapi", "swagger"):
        out.append(Change("request_body_added", True, "high", ep, "request", "(body)", "", "required",
                          f"{ep} now requires a request body"))
    bs, hs = json_schema_of(bbody.get("content")), json_schema_of(hbody.get("content"))
    if bs is not None and hs is not None:
        _walk_or_replaced(ctx, ep, "request", bs, hs, out, b.meta.get("request_type"), h.meta.get("request_type"))
    elif bs is not None and hs is None and hbody:
        out.append(Change("media_type_removed", True, "high", ep, "request", "(body)", "application/json", "",
                          f"{ep} no longer accepts a JSON request body"))

    # responses
    bcodes = {str(c) for c in b.responses}
    hcodes = {str(c) for c in h.responses}
    for code in sorted(bcodes - hcodes):
        if code.startswith("2"):
            out.append(Change("success_response_removed", True, "high", ep, "response", code, code, "",
                              f"Success response {code} was removed from {ep}"))
    for code in sorted(hcodes - bcodes):
        if code.startswith("2"):
            out.append(Change("success_response_added", False, "low", ep, "response", code, "", code,
                              f"New success response {code}; consumers expecting only {sorted(bcodes)} may not handle it"))
    bsucc, hsucc = success_response(b), success_response(h)
    if bsucc and hsucc:
        bsch = json_schema_of((bsucc[1] or {}).get("content"))
        hsch = json_schema_of((hsucc[1] or {}).get("content"))
        if bsch is not None and hsch is None:
            out.append(Change("media_type_removed", True, "high", ep, "response", "(body)", "application/json", "",
                              f"{ep} no longer returns JSON"))
        elif bsch is not None and hsch is not None:
            _walk_or_replaced(ctx, ep, "response", bsch, hsch, out, b.meta.get("response_type"), h.meta.get("response_type"))

    # security
    def schemes(op):
        return sorted({name for req in (op.security or []) for name in (req or {})})
    if schemes(b) != schemes(h):
        breaking = bool(set(schemes(h)) - set(schemes(b)))
        out.append(Change("auth_changed", breaking, "critical" if breaking else "low", ep, "security", "",
                          ", ".join(schemes(b)) or "none", ", ".join(schemes(h)) or "none",
                          f"Authentication for {ep} changed from {schemes(b) or 'none'} to {schemes(h) or 'none'}"))
    del kind


def diff_specs(base: Spec, head: Spec) -> list[Change]:
    ctx = Ctx(head.format, head.strict_inputs or base.strict_inputs)
    kind = NOUNS.get(ctx.fmt, NOUNS["openapi"])[2]
    out: list[Change] = []
    for key in sorted(set(base.operations) - set(head.operations)):
        op = base.operations[key]
        label = f"Endpoint {op.key}" if head.http else op.key
        out.append(Change("endpoint_removed", True, "critical", op.key, "endpoint", "", op.key, "", f"{label} was removed"))
    for key in sorted(set(head.operations) - set(base.operations)):
        op = head.operations[key]
        out.append(Change("endpoint_added", False, "low", op.key, "endpoint", "", "", op.key,
                          f"New {kind} {op.key}" if head.http else f"New {op.key}"))
    for key in sorted(set(base.operations) & set(head.operations)):
        _diff_op(ctx, base.operations[key], head.operations[key], out)
    out.sort(key=lambda c: (not c.breaking, -SEVERITY[c.severity], c.endpoint, c.field))
    return out


def summary(changes: list[Change]) -> dict[str, Any]:
    br = [c for c in changes if c.breaking]
    return {"total": len(changes), "breaking": len(br), "compatible": len(changes) - len(br),
            "worst": max((c.severity for c in br), key=lambda s: SEVERITY[s], default=None)}
