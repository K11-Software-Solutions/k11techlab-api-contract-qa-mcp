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
        return True, f"calls {use.endpoint}"
    if change.location == "response":
        if change.field in ("(body)",) or change.field[:1].isdigit():
            return True, f"reads the response of {use.endpoint}"
        if _field_hit(change.field, use.reads):
            return True, f"reads '{change.field}'" if use.reads is not None else f"reads the response of {use.endpoint}"
        return False, ""
    if change.location == "request":
        if change.rule in ("required_field_added", "field_became_required", "request_body_became_required",
                           "request_body_added", "media_type_removed"):
            return True, f"calls {use.endpoint} without '{change.field}'" if change.field != "(body)" else f"calls {use.endpoint}"
        if _field_hit(change.field, use.sends):
            return True, f"sends '{change.field}'" if use.sends is not None else f"sends a body to {use.endpoint}"
        return False, ""
    if change.location == "parameter":
        return True, f"calls {use.endpoint}"
    return True, f"calls {use.endpoint}"


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


def discover_usage(consumer_dir: Path, spec: Spec, max_hits: int = 200) -> list[dict]:
    pats = [(op, _path_regex(op.path)) for op in spec.operations.values()]
    # longest paths first so /orders/{id}/items doesn't also count as /orders/{id}
    pats.sort(key=lambda x: -len(x[0].path))
    hits: list[dict] = []
    for f in sorted(consumer_dir.rglob("*")):
        if f.suffix not in SOURCE_EXT or any(p in SKIP for p in f.relative_to(consumer_dir).parts):
            continue
        try:
            lines = f.read_text("utf8", errors="replace").splitlines()
        except OSError:
            continue
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


def usage_to_yaml(name: str, hits: list[dict]) -> str:
    eps = sorted({h["endpoint"] for h in hits})
    doc = {"consumers": [{"name": name, "criticality": "medium",
                          "uses": [{"endpoint": e} for e in eps]}]}
    return yaml.safe_dump(doc, sort_keys=False)
