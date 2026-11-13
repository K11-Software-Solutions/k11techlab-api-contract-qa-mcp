"""Classify the differences between two OpenAPI specs as breaking or compatible.

Every change carries the rule it matched, the endpoint and field path, before/after
values and a severity, so a report can show exactly why a change is breaking.
Rules follow the K11 cross-repository contract testing research (IJECS 2026),
extended to $refs, nested objects and array items.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .spec import Operation, Spec, json_schema_of, success_response

SEVERITY = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}


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


def _walk(ep: str, where: str, base: dict, head: dict, path: str, out: list[Change], direction: str, depth: int = 0):
    """Compare two schemas. direction: 'request' (client writes) or 'response' (client reads)."""
    if depth > 12 or not isinstance(base, dict) or not isinstance(head, dict):
        return
    bt, ht = _type(base), _type(head)
    label = path or "(body)"
    if bt and ht and bt != ht and "composite" not in (bt, ht):
        out.append(Change("type_changed", True, "high", ep, where, label, bt, ht,
                          f"{where.capitalize()} field '{label}' changed type from {bt} to {ht}"))
        return
    # nullable / enum
    b_enum, h_enum = base.get("enum"), head.get("enum")
    if isinstance(b_enum, list) and isinstance(h_enum, list):
        removed = [v for v in b_enum if v not in h_enum]
        added = [v for v in h_enum if v not in b_enum]
        if direction == "request" and removed:
            out.append(Change("enum_value_removed", True, "high", ep, where, label, str(b_enum), str(h_enum),
                              f"Request field '{label}' no longer accepts {removed}"))
        elif direction == "response" and added:
            out.append(Change("enum_value_added", False, "low", ep, where, label, str(b_enum), str(h_enum),
                              f"Response field '{label}' may now return {added}; check consumers that switch on it"))
        elif direction == "response" and removed:
            out.append(Change("enum_value_removed", False, "info", ep, where, label, str(b_enum), str(h_enum),
                              f"Response field '{label}' no longer returns {removed}"))
    elif direction == "request" and isinstance(h_enum, list) and b_enum is None:
        out.append(Change("enum_restricted", True, "medium", ep, where, label, "any", str(h_enum),
                          f"Request field '{label}' is now restricted to {h_enum}"))
    if direction == "response" and head.get("nullable") and not base.get("nullable"):
        out.append(Change("became_nullable", True, "medium", ep, where, label, "non-null", "nullable",
                          f"Response field '{label}' can now be null"))

    if (bt or ht) in ("array",) or "items" in base or "items" in head:
        _walk(ep, where, base.get("items") or {}, head.get("items") or {}, f"{path}[]", out, direction, depth + 1)
        return

    bp, hp = _props(base), _props(head)
    if not bp and not hp:
        return
    breq, hreq = _required(base), _required(head)
    for name in sorted(set(bp) | set(hp)):
        fpath = f"{path}.{name}" if path else name
        if name in bp and name not in hp:
            if direction == "response":
                out.append(Change("field_removed", True, "high", ep, where, fpath, _type(bp[name]), "",
                                  f"Response field '{fpath}' was removed"))
            else:
                out.append(Change("field_removed", name in breq, "medium" if name in breq else "low", ep, where,
                                  fpath, _type(bp[name]), "",
                                  f"Request field '{fpath}' was removed" + ("" if name in breq else
                                  "; clients still sending it may be rejected if the server forbids unknown fields")))
        elif name in hp and name not in bp:
            if direction == "request" and name in hreq:
                out.append(Change("required_field_added", True, "high", ep, where, fpath, "", _type(hp[name]),
                                  f"New required request field '{fpath}'"))
            else:
                out.append(Change("field_added", False, "low", ep, where, fpath, "", _type(hp[name]),
                                  f"New optional {where} field '{fpath}'"))
        else:
            if direction == "request" and name in hreq and name not in breq:
                out.append(Change("field_became_required", True, "high", ep, where, fpath, "optional", "required",
                                  f"Request field '{fpath}' became required"))
            if direction == "response" and name in breq and name not in hreq:
                out.append(Change("field_no_longer_guaranteed", True, "medium", ep, where, fpath, "required",
                                  "optional", f"Response field '{fpath}' is no longer guaranteed (required → optional)"))
            _walk(ep, where, bp[name], hp[name], fpath, out, direction, depth + 1)


def _param_map(op: Operation) -> dict[tuple[str, str], dict]:
    return {(p.get("in", ""), p.get("name", "")): p for p in op.parameters}


def _diff_op(b: Operation, h: Operation, out: list[Change]) -> None:
    ep = h.key
    if h.deprecated and not b.deprecated:
        out.append(Change("deprecated", False, "info", ep, "endpoint", description=f"{ep} is now deprecated"))
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
            _walk(ep, "parameter", bpar[key].get("schema") or {}, hpar[key].get("schema") or {}, label, out, "request")
    hpath = [p for (loc, _), p in hpar.items() if loc == "path"]
    for bp_, hp_ in zip(bpath, hpath):
        _walk(ep, "parameter", bp_.get("schema") or {}, hp_.get("schema") or {}, f"path:{hp_.get('name')}", out, "request")

    # request body
    bbody, hbody = b.request_body or {}, h.request_body or {}
    if hbody.get("required") and not bbody.get("required") and bbody:
        out.append(Change("request_body_became_required", True, "high", ep, "request", "(body)", "optional", "required",
                          f"Request body of {ep} became required"))
    elif hbody and not bbody and hbody.get("required"):
        out.append(Change("request_body_added", True, "high", ep, "request", "(body)", "", "required",
                          f"{ep} now requires a request body"))
    bs, hs = json_schema_of(bbody.get("content")), json_schema_of(hbody.get("content"))
    if bs is not None and hs is not None:
        _walk(ep, "request", bs, hs, "", out, "request")
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
        elif bsch and hsch:
            _walk(ep, "response", bsch, hsch, "", out, "response")

    # security
    def schemes(op):
        return sorted({name for req in (op.security or []) for name in (req or {})})
    if schemes(b) != schemes(h):
        breaking = bool(schemes(h)) and set(schemes(h)) - set(schemes(b)) != set()
        out.append(Change("auth_changed", breaking, "critical" if breaking else "low", ep, "security", "",
                          ", ".join(schemes(b)) or "none", ", ".join(schemes(h)) or "none",
                          f"Authentication for {ep} changed from {schemes(b) or 'none'} to {schemes(h) or 'none'}"))


def diff_specs(base: Spec, head: Spec) -> list[Change]:
    out: list[Change] = []
    for key in sorted(set(base.operations) - set(head.operations)):
        op = base.operations[key]
        out.append(Change("endpoint_removed", True, "critical", op.key, "endpoint", "", op.key, "",
                          f"Endpoint {op.key} was removed"))
    for key in sorted(set(head.operations) - set(base.operations)):
        op = head.operations[key]
        out.append(Change("endpoint_added", False, "low", op.key, "endpoint", "", "", op.key, f"New endpoint {op.key}"))
    for key in sorted(set(base.operations) & set(head.operations)):
        _diff_op(base.operations[key], head.operations[key], out)
    out.sort(key=lambda c: (not c.breaking, -SEVERITY[c.severity], c.endpoint, c.field))
    return out


def summary(changes: list[Change]) -> dict[str, Any]:
    br = [c for c in changes if c.breaking]
    return {"total": len(changes), "breaking": len(br), "compatible": len(changes) - len(br),
            "worst": max((c.severity for c in br), key=lambda s: SEVERITY[s], default=None)}
