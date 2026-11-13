"""Breaking-change rules, consumer impact and discovery (no network)."""

import copy
from pathlib import Path

import pytest

from k11_contract_qa import consumers
from k11_contract_qa.diff import diff_specs
from k11_contract_qa.spec import SpecError, build_spec, load_spec, parse_spec

BASE = {
    "openapi": "3.0.3", "info": {"title": "T", "version": "1"},
    "paths": {
        "/things/{thingId}": {
            "get": {
                "parameters": [{"name": "thingId", "in": "path", "required": True, "schema": {"type": "string"}},
                               {"name": "expand", "in": "query", "schema": {"type": "string"}}],
                "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}}}},
            },
            "put": {
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object", "required": ["name"],
                    "properties": {"name": {"type": "string"}, "kind": {"type": "string", "enum": ["a", "b"]},
                                   "tags": {"type": "array", "items": {"type": "string"}}}}}}},
                "responses": {"200": {"description": "ok"}},
            },
        }
    },
    "components": {"schemas": {"Thing": {"type": "object", "required": ["id", "size"], "properties": {
        "id": {"type": "string"}, "size": {"type": "integer"},
        "owner": {"type": "object", "properties": {"email": {"type": "string"}, "name": {"type": "string"}}},
        "parts": {"type": "array", "items": {"type": "object", "properties": {"w": {"type": "number"}}}}}}}},
}


def rules(base, head):
    return {(c.rule, c.field, c.breaking) for c in diff_specs(build_spec(base), build_spec(head))}


def mutate(fn):
    head = copy.deepcopy(BASE)
    fn(head)
    return head


def test_no_changes_and_path_param_rename_is_compatible():
    renamed = mutate(lambda h: h["paths"].__setitem__("/things/{id}", h["paths"].pop("/things/{thingId}")))
    renamed["paths"]["/things/{id}"]["get"]["parameters"][0]["name"] = "id"
    assert rules(BASE, BASE) == set()
    assert rules(BASE, renamed) == set()


@pytest.mark.parametrize("change,expected", [
    (lambda h: h["components"]["schemas"]["Thing"]["properties"].pop("size"), ("field_removed", "size", True)),
    (lambda h: h["components"]["schemas"]["Thing"]["properties"]["size"].update(type="string"), ("type_changed", "size", True)),
    (lambda h: h["components"]["schemas"]["Thing"]["properties"]["owner"]["properties"].pop("email"), ("field_removed", "owner.email", True)),
    (lambda h: h["components"]["schemas"]["Thing"]["properties"]["parts"]["items"]["properties"]["w"].update(type="string"), ("type_changed", "parts[].w", True)),
    (lambda h: h["components"]["schemas"]["Thing"]["required"].remove("size"), ("field_no_longer_guaranteed", "size", True)),
    (lambda h: h["components"]["schemas"]["Thing"]["properties"].update(extra={"type": "string"}), ("field_added", "extra", False)),
    (lambda h: h["paths"]["/things/{thingId}"]["put"]["requestBody"]["content"]["application/json"]["schema"]["required"].append("kind"), ("field_became_required", "kind", True)),
    (lambda h: h["paths"]["/things/{thingId}"]["put"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["kind"]["enum"].remove("b"), ("enum_value_removed", "kind", True)),
    (lambda h: h["paths"]["/things/{thingId}"]["get"]["parameters"][1].update(required=True), ("parameter_became_required", "query:expand", True)),
    (lambda h: h["paths"]["/things/{thingId}"]["get"]["responses"].update({"200": h["paths"]["/things/{thingId}"]["get"]["responses"].pop("200")}) or h["paths"]["/things/{thingId}"].pop("put"), ("endpoint_removed", "", True)),
    (lambda h: h["paths"]["/things/{thingId}"]["get"].update(security=[{"apiKey": []}]), ("auth_changed", "", True)),
])
def test_rules(change, expected):
    assert expected in rules(BASE, mutate(change))


def test_spec_errors(tmp_path):
    with pytest.raises(SpecError, match="not an OpenAPI 3"):
        parse_spec('{"swagger": "2.0"}')
    with pytest.raises(SpecError, match="inside the project"):
        load_spec(tmp_path, "../outside.yaml")
    (tmp_path / "r.yaml").write_text("openapi: 3.0.0\ninfo: {title: R, version: '1'}\npaths: {}\n"
                                     "components: {schemas: {N: {type: object, properties: {next: {$ref: '#/components/schemas/N'}}}}}\n")
    assert load_spec(tmp_path, "r.yaml").title == "R"


def test_demo_impact_is_field_precise(project):
    base, head = load_spec(project, "openapi.v1.yaml"), load_spec(project, "openapi.yaml")
    changes = diff_specs(base, head)
    assert sum(c.breaking for c in changes) == 6
    impact = consumers.assess(changes, consumers.load_consumers(project / ".qa-mcp/consumers.yaml"))
    assert impact["verdict"] == "breaking" and impact["needs_human"]
    broken = {b["consumer"]: [(c["rule"], c["field"]) for c in b["changes"]] for b in impact["consumers_broken"]}
    assert broken == {
        "billing-svc": [("type_changed", "total")],
        "notification-svc": [("field_removed", "customer.email")],
        "checkout-web": [("required_field_added", "channel")],
    }
    # DELETE was removed (nobody calls it); POST /orders response changes don't touch checkout-web,
    # which only reads 'id' from that response
    unused = {(c["rule"], c["endpoint"], c["field"]) for c in impact["breaking_without_known_consumers"]}
    assert unused == {("endpoint_removed", "DELETE /orders/{orderId}", ""),
                      ("field_removed", "POST /orders", "customer.email"),
                      ("type_changed", "POST /orders", "total")}
    assert "analytics-svc" not in broken  # reads only id and status


def test_compatible_change_verdict():
    impact = consumers.assess(diff_specs(build_spec(BASE), build_spec(mutate(
        lambda h: h["components"]["schemas"]["Thing"]["properties"].update(extra={"type": "string"})))), [])
    assert impact["verdict"] == "compatible" and not impact["needs_human"]


def test_discovery_finds_real_calls_only(project):
    head = load_spec(project, "openapi.yaml")
    found = {n: sorted({h["endpoint"] for h in consumers.discover_usage(project / "consumers" / n, head)})
             for n in ("notification-svc", "billing-svc", "checkout-web", "analytics-svc")}
    assert found == {
        "notification-svc": ["GET /orders/{id}"],
        "billing-svc": ["GET /orders/{id}"],
        "checkout-web": ["GET /orders/{id}/tracking", "POST /orders"],
        "analytics-svc": ["GET /orders"],
    }
