"""AsyncAPI 2.x / 3.x -> Spec.

Each message a service sends or receives on a channel is an operation:
  ``EMITS order.created``        the service sends it; consumers read it  (response rules)
  ``RECEIVES order.cancel``      the service receives it; producers write it (request rules)
When a channel carries several messages, each is its own operation: ``EMITS orders#OrderCreated``.

AsyncAPI 2.x: ``subscribe`` = the application sends, ``publish`` = it receives.
AsyncAPI 3.x: operations with ``action: send`` / ``receive``.
"""

from __future__ import annotations

from typing import Any

from ..spec import Spec, SpecError, make_op, resolve


def _messages(spec: dict, node: Any) -> list[tuple[str, dict]]:
    """[(message name, message)] from a message / oneOf / list / map."""
    node = resolve(spec, node) if isinstance(node, (dict, list)) else node
    if isinstance(node, list):
        return [m for n in node for m in _messages(spec, n)]
    if not isinstance(node, dict):
        return []
    if "oneOf" in node:
        return [m for n in node["oneOf"] for m in _messages(spec, n)]
    if "payload" in node or "headers" in node:
        name = node.get("name") or node.get("x-ref-name") or node.get("messageId") or node.get("title") or ""
        return [(str(name), node)]
    # 3.x channel/operation message maps: {key: message}
    out = []
    for key, val in node.items():
        if isinstance(val, dict) and ("payload" in val or "headers" in val):
            out.append((str(val.get("name") or val.get("x-ref-name") or key), val))
    return out


def _payload(msg: dict) -> dict:
    p = msg.get("payload")
    return p if isinstance(p, dict) else {}


def build(doc: dict, label: str) -> Spec:
    version = str(doc.get("asyncapi", ""))
    if not version[:1] in ("2", "3"):
        raise SpecError(f"{label}: AsyncAPI {version} is not supported (2.x and 3.x are)")
    entries: list[tuple[str, str, str, dict, dict]] = []  # (action, channel, message name, message, op info)
    if version.startswith("2"):
        for channel, item in (doc.get("channels") or {}).items():
            item = resolve(doc, item)
            for key, action in (("subscribe", "EMITS"), ("publish", "RECEIVES")):
                op = item.get(key)
                if isinstance(op, dict):
                    for name, msg in _messages(doc, op.get("message")):
                        entries.append((action, channel, name, msg, op))
    else:
        channels = doc.get("channels") or {}
        for op_id, op in (doc.get("operations") or {}).items():
            op = op if isinstance(op, dict) else {}
            action = {"send": "EMITS", "receive": "RECEIVES"}.get(op.get("action", ""), "")
            if not action:
                continue
            ch_ref = (op.get("channel") or {}).get("$ref", "")
            ch_key = ch_ref.split("/")[-1] if ch_ref else ""
            channel = resolve(doc, channels.get(ch_key, {})) if ch_key else {}
            address = channel.get("address") or ch_key or op_id
            msgs = _messages(doc, op.get("messages")) or _messages(doc, channel.get("messages"))
            for name, msg in msgs:
                entries.append((action, str(address), name, msg, {**op, "operationId": op_id}))
    per_channel: dict[tuple[str, str], int] = {}
    for action, channel, *_ in entries:
        per_channel[(action, channel)] = per_channel.get((action, channel), 0) + 1
    ops = {}
    for action, channel, name, msg, op in entries:
        path = f"{channel}#{name}" if per_channel[(action, channel)] > 1 and name else channel
        payload = _payload(msg)
        content_type = msg.get("contentType") or doc.get("defaultContentType") or "application/json"
        kwargs: dict[str, Any] = {"summary": op.get("summary", "") or msg.get("summary", ""),
                                  "meta": {"content_type": content_type},
                                  "deprecated": bool(msg.get("deprecated") or op.get("deprecated"))}
        if action == "EMITS":
            o = make_op(action, path, response=payload, **kwargs)
        else:
            o = make_op(action, path, request=payload, **kwargs)
        o.operation_id = op.get("operationId", "")
        ops[o.norm_key] = o
    info = doc.get("info", {})
    return Spec(doc, label, ops, "asyncapi", info={"title": info.get("title", "Event API"), "version": info.get("version", "")})
