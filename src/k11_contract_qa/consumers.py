"""Consumers: who calls which endpoint, and which fields they read or send.

Declared in ``.qa-mcp/consumers.yaml``:

    consumers:
      - name: notification-svc
        repo: K11-Software-Solutions/k11-notification-svc
        owner: "@team-notify"
        criticality: high            # critical | high | medium | low
        uses:
          - endpoint: GET /orders/{id}
            reads: [id, status, total]   # response fields used (optional: omit = all)
          - endpoint: POST /orders
            sends: [items, customerId]   # request fields sent (optional)

``discover_usage`` scans a consumer's source tree for calls to the provider's paths,
with file and line as evidence, to help write (or check) that file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .diff import SEVERITY, Change
from .spec import Spec, norm_path

ESCALATE_CONSUMERS = 2  # from the cross-repository HITL gate: 2+ broken consumers → a person decides


@dataclass
class Use:
    endpoint: str
    reads: list[str] | None = None
    sends: list[str] | None = None


@dataclass
class Consumer:
    name: str
    repo: str = ""
    owner: str = ""
    criticality: str = "medium"
    uses: list[Use] = field(default_factory=list)


def load_consumers(path: Path) -> list[Consumer]:
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text("utf8")) or {}
    out = []
    for c in data.get("consumers", []) or []:
        uses = [Use(u["endpoint"], u.get("reads"), u.get("sends")) for u in c.get("uses", []) if u.get("endpoint")]
        out.append(Consumer(c["name"], c.get("repo", ""), c.get("owner", ""), c.get("criticality", "medium"), uses))
    return out


def _same_endpoint(a: str, b: str) -> bool:
    am, _, ap = a.strip().partition(" ")
    bm, _, bp = b.strip().partition(" ")
    return am.upper() == bm.upper() and norm_path(ap.strip()) == norm_path(bp.strip())


def _field_hit(change_field: str, fields: list[str] | None) -> bool:
    if fields is None:
        return True  # consumer didn't say which fields: assume all
    cf = change_field.replace("[]", "")
    return any(cf == f or cf.startswith(f + ".") or f.startswith(cf + ".") for f in (x.replace("[]", "") for x in fields))


def affects(change: Change, use: Use) -> tuple[bool, str]:
    """Does this change break this use? Returns (hit, why)."""
    if not _same_endpoint(change.endpoint, use.endpoint):
        return False, ""
    if change.location in ("endpoint", "security"):
        return True, f"uses {use.endpoint}"
    whole = "(no field list, so every field counts)"
    if change.location == "response":
        if change.field in ("(body)", "(message)") or change.field[:1].isdigit():
            return True, f"reads the output of {use.endpoint}"
        if _field_hit(change.field, use.reads):
            return True, f"reads '{change.field}'" if use.reads is not None else f"reads the output {whole}"
        return False, ""
    if change.location == "request":
        if change.rule in ("required_field_added", "field_became_required", "request_body_became_required",
                           "request_body_added", "media_type_removed", "message_replaced"):
            return True, (f"doesn't send the now-required '{change.field}'"
                          if change.field not in ("(body)", "(message)") else f"sends input to {use.endpoint}")
        if _field_hit(change.field, use.sends):
            return True, f"sends '{change.field}'" if use.sends is not None else f"sends input {whole}"
        return False, ""
    return True, f"uses {use.endpoint}"


def assess(changes: list[Change], consumers: list[Consumer]) -> dict[str, Any]:
    """Verdict + per-consumer evidence. Deterministic; no LLM."""
    breaking = [c for c in changes if c.breaking]
    broken: dict[str, dict] = {}
    unused: list[dict] = []
    for ch in breaking:
        hit_any = False
        for con in consumers:
            for use in con.uses:
                hit, why = affects(ch, use)
                if hit:
                    hit_any = True
                    entry = broken.setdefault(con.name, {"consumer": con.name, "repo": con.repo, "owner": con.owner,
                                                         "criticality": con.criticality, "changes": []})
                    entry["changes"].append({**ch.to_dict(), "why": why})
                    break
        if not hit_any:
            unused.append(ch.to_dict())
    critical = [b["consumer"] for b in broken.values() if b["criticality"] in ("critical", "high")]
    if broken:
        verdict = "breaking"
    elif breaking:
        verdict = "breaking_no_known_consumers"
    else:
        verdict = "compatible"
    escalate = len(broken) >= ESCALATE_CONSUMERS or bool(critical)
    reasons = []
    if len(broken) >= ESCALATE_CONSUMERS:
        reasons.append(f"{len(broken)} consumers break (threshold {ESCALATE_CONSUMERS})")
    if critical:
        reasons.append(f"high-criticality consumer(s) break: {', '.join(critical)}")
    worst = max((c.severity for c in breaking), key=lambda s: SEVERITY[s], default=None)
    return {
        "verdict": verdict,
        "needs_human": escalate,
        "needs_human_reason": "; ".join(reasons),
        "breaking_changes": len(breaking),
        "worst_severity": worst,
        "consumers_known": len(consumers),
        "consumers_broken": sorted(broken.values(), key=lambda b: (-SEVERITY.get(b["criticality"], 2), b["consumer"])),
        "breaking_without_known_consumers": unused,
    }


# ---- discovery ----------------------------------------------------------------

SOURCE_EXT = {".py", ".java", ".kt", ".ts", ".tsx", ".js", ".jsx", ".go", ".cs", ".rb", ".php", ".scala"}
SKIP = {"node_modules", ".git", ".venv", "venv", "target", "build", "dist", "__pycache__", ".gradle", ".idea"}
HTTP_HINT = re.compile(r"\b(get|post|put|patch|delete)\b", re.I)


def _path_regex(path: str) -> re.Pattern:
    parts = re.split(r"\{[^}]+\}", path.rstrip("/"))
    # a template slot matches literals, f-string/JS ${} / Java "+id+" style pieces
    slot = r"""(?:\{[^}]*\}|\$\{[^}]*\}|%[sd]|:[A-Za-z_]\w*|[A-Za-z0-9_\-]+|["']\s*\+\s*[\w.()]+\s*\+\s*["']|["']\s*\+\s*[\w.()]+)"""
    return re.compile(slot.join(re.escape(p) for p in parts) + r"""(?=["'/?`\s)]|$)""")


def _source_files(consumer_dir: Path, extra: tuple[str, ...] = ()):
    for f in sorted(consumer_dir.rglob("*")):
        if not f.is_file() or (f.suffix not in SOURCE_EXT and f.suffix not in extra):
            continue
        if any(p in SKIP for p in f.relative_to(consumer_dir).parts):
            continue
        try:
            yield f, f.read_text("utf8", errors="replace")
        except OSError:
            continue


def discover_usage(consumer_dir: Path, spec: Spec, max_hits: int = 200) -> list[dict]:
    """Calls to the provider found in a consumer's code, with file and line, for any contract format."""
    if spec.format == "graphql":
        return _discover_graphql(consumer_dir, spec, max_hits)
    if spec.format in ("protobuf", "wsdl"):
        return _discover_calls(consumer_dir, spec, max_hits)
    if spec.format == "asyncapi":
        return _discover_events(consumer_dir, spec, max_hits)
    pats = [(op, _path_regex(op.path)) for op in spec.operations.values()]
    # longest paths first so /orders/{id}/items doesn't also count as /orders/{id}
    pats.sort(key=lambda x: -len(x[0].path))
    hits: list[dict] = []
    for f, text in _source_files(consumer_dir):
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            spans: list[tuple[int, int, str]] = []  # (start, end, normalized path) already matched on this line
            for op, rx in pats:
                m = rx.search(line)
                if not m:
                    continue
                np_ = norm_path(op.path)
                # a longer path already matched here (e.g. /orders/{id}/tracking) covers this one (/orders)
                if any(s <= m.start() and m.end() <= e and p != np_ for s, e, p in spans):
                    continue
                window = " ".join(lines[max(0, i - 3):i + 1])
                methods = {x.lower() for x in HTTP_HINT.findall(window)}
                if methods and op.method not in methods:
                    continue
                spans.append((m.start(), m.end() + 64, np_))
                hits.append({"endpoint": op.key, "file": f.relative_to(consumer_dir).as_posix(), "line": i,
                             "code": line.strip()[:160], "method_evidence": bool(methods)})
                if len(hits) >= max_hits:
                    return hits
    return hits


def _discover_graphql(consumer_dir: Path, spec: Spec, max_hits: int) -> list[dict]:
    """Parse the consumer's actual query documents: exact fields read and arguments sent."""
    from .formats.gql import documents_in, usage

    hits = []
    for f, text in _source_files(consumer_dir, (".graphql", ".gql")):
        for line, doc in documents_in(text, f.suffix):
            for u in usage(doc):
                if spec.find(u["endpoint"]) is None:
                    continue
                hits.append({"endpoint": u["endpoint"], "file": f.relative_to(consumer_dir).as_posix(), "line": line,
                             "code": " ".join(doc.split())[:160], "reads": u["reads"], "sends": u["sends"],
                             "method_evidence": True})
                if len(hits) >= max_hits:
                    return hits
    return hits


def _camel_variants(name: str) -> list[str]:
    return sorted({name, name[:1].lower() + name[1:], name[:1].upper() + name[1:],
                   re.sub(r"(?<!^)([A-Z])", r"_\1", name).lower()})


def _discover_calls(consumer_dir: Path, spec: Spec, max_hits: int) -> list[dict]:
    """gRPC stubs and SOAP clients: a method call named after the RPC/operation, in a file that
    also mentions the service (stub/client/port) or SOAP/WSDL."""
    targets = []
    for op in spec.operations.values():
        service, _, method = op.path.rpartition("/")
        service = service.split(".")[-1]
        rx = re.compile(r"\.(?:%s)(?:Async)?\s*\(" % "|".join(map(re.escape, _camel_variants(method))))
        hint = re.compile("|".join([re.escape(service), re.escape(service.replace("Service", "")) + r"\w*(?:Stub|Client|Port)"]
                                   + ([r"wsdl", r"soap", r"zeep", r"JAX-?WS", r"@WebServiceRef"] if spec.format == "wsdl" else
                                      [r"grpc", r"_pb2", r"Stub\b", r"\.proto"])), re.I)
        targets.append((op, rx, hint))
    hits = []
    for f, text in _source_files(consumer_dir):
        lines = text.splitlines()
        for op, rx, hint in targets:
            if not hint.search(text):
                continue
            for i, line in enumerate(lines, 1):
                if rx.search(line):
                    hits.append({"endpoint": op.key, "file": f.relative_to(consumer_dir).as_posix(), "line": i,
                                 "code": line.strip()[:160], "method_evidence": True})
                    if len(hits) >= max_hits:
                        return hits
    return hits


SUBSCRIBE_HINT = re.compile(r"\b(subscribe|consume|consumer|listen|listener|poll|KafkaListener|RabbitListener|Incoming)\b", re.I)
PUBLISH_HINT = re.compile(r"\b(publish|produce|producer|send|emit|KafkaTemplate)\b", re.I)


def _discover_events(consumer_dir: Path, spec: Spec, max_hits: int) -> list[dict]:
    """Topic/channel names in the consumer's code. A consumer subscribing to what the service EMITS reads
    it; one publishing what the service RECEIVES writes it."""
    by_channel: dict[str, list] = {}
    for op in spec.operations.values():
        by_channel.setdefault(op.path.split("#")[0], []).append(op)
    hits = []
    for f, text in _source_files(consumer_dir):
        lines = text.splitlines()
        for channel, ops in by_channel.items():
            rx = re.compile(r"""["'`]%s["'`]""" % re.escape(channel))
            for i, line in enumerate(lines, 1):
                if not rx.search(line):
                    continue
                window = " ".join(lines[max(0, i - 4):i + 2])
                sub, pub = bool(SUBSCRIBE_HINT.search(window)), bool(PUBLISH_HINT.search(window))
                for op in ops:
                    if (op.method == "EMITS" and pub and not sub) or (op.method == "RECEIVES" and sub and not pub):
                        continue
                    hits.append({"endpoint": op.key, "file": f.relative_to(consumer_dir).as_posix(), "line": i,
                                 "code": line.strip()[:160], "method_evidence": sub or pub})
                    if len(hits) >= max_hits:
                        return hits
    return hits


def usage_to_yaml(name: str, hits: list[dict]) -> str:
    uses: dict[str, dict] = {}
    for h in hits:
        u = uses.setdefault(h["endpoint"], {"endpoint": h["endpoint"]})
        for key in ("reads", "sends"):
            if h.get(key):
                u[key] = sorted(set(u.get(key, [])) | set(h[key]))
    doc = {"consumers": [{"name": name, "criticality": "medium", "uses": [uses[e] for e in sorted(uses)]}]}
    return yaml.safe_dump(doc, sort_keys=False)
