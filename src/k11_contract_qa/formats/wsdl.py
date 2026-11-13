"""SOAP / WSDL 1.1 -> Spec.

Each portType operation is an operation ``SOAP <PortType>/<Operation>``. Input
and output messages are built from their parts: ``element=`` parts follow the
embedded XSD (sequence/all/choice, minOccurs/maxOccurs, nillable, enumerations,
complexContent extension); ``type=`` parts (RPC style) map directly. Binding
details (SOAPAction, style) are compared as part of the signature. SOAP
services usually validate strictly, so the spec is ``strict_inputs``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from ..spec import Spec, SpecError, make_op

WSDL = "http://schemas.xmlsoap.org/wsdl/"
XSD = "http://www.w3.org/2001/XMLSchema"
SOAP_NS = ("http://schemas.xmlsoap.org/wsdl/soap/", "http://schemas.xmlsoap.org/wsdl/soap12/")
BUILTIN = {
    "string": ("string", None), "normalizedString": ("string", None), "token": ("string", None),
    "anyURI": ("string", "uri"), "QName": ("string", None), "ID": ("string", None),
    "int": ("integer", "int32"), "integer": ("integer", None), "long": ("integer", "int64"),
    "short": ("integer", "int16"), "byte": ("integer", "int8"), "nonNegativeInteger": ("integer", None),
    "positiveInteger": ("integer", None), "unsignedInt": ("integer", "uint32"), "unsignedLong": ("integer", "uint64"),
    "unsignedShort": ("integer", "uint16"), "decimal": ("number", "decimal"), "float": ("number", "float"),
    "double": ("number", "double"), "boolean": ("boolean", None), "date": ("string", "date"),
    "dateTime": ("string", "date-time"), "time": ("string", "time"), "duration": ("string", "duration"),
    "base64Binary": ("string", "byte"), "hexBinary": ("string", "hex"), "anyType": ("", None),
}
MAX_DEPTH = 10


def _local(q: str | None) -> str:
    return (q or "").split(":")[-1]


class _Xsd:
    def __init__(self, root: ET.Element):
        self.elements: dict[str, ET.Element] = {}
        self.types: dict[str, ET.Element] = {}
        for schema in root.iter(f"{{{XSD}}}schema"):
            for child in schema:
                name = child.get("name")
                if not name:
                    continue
                if child.tag == f"{{{XSD}}}element":
                    self.elements[name] = child
                elif child.tag in (f"{{{XSD}}}complexType", f"{{{XSD}}}simpleType"):
                    self.types[name] = child

    def type_schema(self, qname: str, depth: int, seen: frozenset) -> dict:
        name = _local(qname)
        if name in BUILTIN and name not in self.types:
            t, f = BUILTIN[name]
            return {"type": t, **({"format": f} if f else {})} if t else {}
        node = self.types.get(name)
        if node is None:
            return {"x-unresolved-ref": name}
        if name in seen or depth > MAX_DEPTH:
            return {"type": "object", "x-recursive-ref": name}
        return {**self.node_schema(node, depth + 1, seen | {name}), "x-ref-name": name}

    def node_schema(self, node: ET.Element, depth: int, seen: frozenset) -> dict:
        tag = node.tag.split("}")[-1]
        if tag == "simpleType":
            r = node.find(f"{{{XSD}}}restriction")
            if r is not None:
                base = self.type_schema(r.get("base", "string"), depth, seen)
                enums = [e.get("value") for e in r.findall(f"{{{XSD}}}enumeration")]
                return {**base, **({"enum": enums} if enums else {})}
            return {"type": "string"}
        if tag == "complexType":
            props: dict[str, dict] = {}
            req: list[str] = []
            cc = node.find(f"{{{XSD}}}complexContent")
            if cc is not None:
                ext = cc.find(f"{{{XSD}}}extension")
                if ext is not None:
                    base = self.type_schema(ext.get("base", ""), depth, seen)
                    props.update(base.get("properties") or {})
                    req += base.get("required") or []
                    node = ext
            for group in ("sequence", "all", "choice"):
                for g in node.findall(f"{{{XSD}}}{group}"):
                    self._particles(g, props, req, depth, seen, optional=(group == "choice"))
            for attr in node.findall(f"{{{XSD}}}attribute"):
                aname = attr.get("name") or _local(attr.get("ref"))
                props[f"@{aname}"] = self.type_schema(attr.get("type", "string"), depth, seen)
                if attr.get("use") == "required":
                    req.append(f"@{aname}")
            return {"type": "object", "properties": props, **({"required": req} if req else {})}
        if tag == "element":
            return self.element_schema(node, depth, seen)[1]
        return {}

    def _particles(self, group: ET.Element, props: dict, req: list, depth: int, seen: frozenset, optional: bool):
        for child in group:
            ctag = child.tag.split("}")[-1]
            if ctag == "element":
                name, schema = self.element_schema(child, depth, seen)
                props[name] = schema
                if not optional and child.get("minOccurs", "1") != "0":
                    req.append(name)
            elif ctag in ("sequence", "choice", "all"):
                self._particles(child, props, req, depth, seen, optional or ctag == "choice")

    def element_schema(self, el: ET.Element, depth: int, seen: frozenset) -> tuple[str, dict]:
        if el.get("ref"):
            target = self.elements.get(_local(el.get("ref")))
            name = _local(el.get("ref"))
            schema = self.element_schema(target, depth + 1, seen)[1] if target is not None else {"x-unresolved-ref": name}
        else:
            name = el.get("name", "")
            if el.get("type"):
                schema = self.type_schema(el.get("type"), depth, seen)
            else:
                inner = el.find(f"{{{XSD}}}complexType")
                if inner is None:
                    inner = el.find(f"{{{XSD}}}simpleType")
                schema = self.node_schema(inner, depth + 1, seen) if inner is not None else {"type": "string"}
        if el.get("nillable") == "true":
            schema = {**schema, "nullable": True}
        max_occurs = el.get("maxOccurs", "1")
        if max_occurs == "unbounded" or (max_occurs.isdigit() and int(max_occurs) > 1):
            schema = {"type": "array", "items": schema}
        return name, schema


def build(text: str, label: str) -> Spec:
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError as e:
        raise SpecError(f"{label} is not valid XML: {e}") from None
    if root.tag != f"{{{WSDL}}}definitions":
        if root.tag.endswith("}description"):
            raise SpecError(f"{label}: WSDL 2.0 is not supported yet (WSDL 1.1 is)")
        raise SpecError(f"{label} is not a WSDL 1.1 document")
    xsd = _Xsd(root)
    messages: dict[str, list[ET.Element]] = {m.get("name"): m.findall(f"{{{WSDL}}}part")
                                            for m in root.findall(f"{{{WSDL}}}message")}

    def message_schema(qname: str | None) -> dict:
        parts = messages.get(_local(qname), [])
        if len(parts) == 1 and parts[0].get("element"):
            el = xsd.elements.get(_local(parts[0].get("element")))
            return xsd.element_schema(el, 0, frozenset())[1] if el is not None else {"x-unresolved-ref": parts[0].get("element")}
        props = {}
        for p in parts:
            if p.get("element"):
                el = xsd.elements.get(_local(p.get("element")))
                props[p.get("name")] = xsd.element_schema(el, 0, frozenset())[1] if el is not None else {}
            else:
                props[p.get("name")] = xsd.type_schema(p.get("type", "string"), 0, frozenset())
        return {"type": "object", "properties": props, "required": list(props)}

    binding_info: dict[tuple[str, str], dict] = {}
    for b in root.findall(f"{{{WSDL}}}binding"):
        pt = _local(b.get("type"))
        style = next((x.get("style") for x in b if x.tag.split("}")[0].strip("{") in SOAP_NS and x.get("style")), "document")
        for op in b.findall(f"{{{WSDL}}}operation"):
            action = next((x.get("soapAction") for x in op if x.tag.split("}")[0].strip("{") in SOAP_NS
                           and x.get("soapAction") is not None), "")
            binding_info[(pt, op.get("name"))] = {"soap_action": action, "soap_style": style}
    ops = {}
    for pt in root.findall(f"{{{WSDL}}}portType"):
        for op in pt.findall(f"{{{WSDL}}}operation"):
            inp, outp = op.find(f"{{{WSDL}}}input"), op.find(f"{{{WSDL}}}output")
            doc = op.find(f"{{{WSDL}}}documentation")
            o = make_op("SOAP", f"{pt.get('name')}/{op.get('name')}",
                        request=message_schema(inp.get("message")) if inp is not None else None,
                        response=message_schema(outp.get("message")) if outp is not None else None,
                        summary=(doc.text or "").strip() if doc is not None else "",
                        meta=binding_info.get((pt.get("name"), op.get("name")), {}))
            ops[o.norm_key] = o
    if not ops:
        raise SpecError(f"{label}: no portType operations found")
    svc = root.find(f"{{{WSDL}}}service")
    title = (svc.get("name") if svc is not None else None) or root.get("name") or "SOAP service"
    return Spec({"targetNamespace": root.get("targetNamespace")}, label, ops, "wsdl", strict_inputs=True,
                info={"title": title, "version": ""})
