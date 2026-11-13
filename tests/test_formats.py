"""Swagger 2.0, GraphQL, protobuf, AsyncAPI and WSDL: rules, discovery, impact and deployed drift."""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from k11_contract_qa import consumers, drift
from k11_contract_qa.cli import main as cli
from k11_contract_qa.diff import diff_specs
from k11_contract_qa.formats import detect, load_text
from k11_contract_qa.spec import SpecError, load_spec

FORMATS = Path(__file__).resolve().parents[1] / "demo" / "formats"


def changes(fmt, base, head):
    root = FORMATS / fmt
    b, h = load_spec(root, base), load_spec(root, head)
    return h, diff_specs(b, h)


def breaking(chs):
    return {(c.rule, c.endpoint.split(" ", 1)[1], c.field) for c in chs if c.breaking}


def impact_from_discovery(fmt, head, chs, names):
    found = []
    for n in names:
        hits = consumers.discover_usage(FORMATS / fmt / "consumers" / n, head)
        entry = yaml.safe_load(consumers.usage_to_yaml(n, hits))["consumers"][0]
        found.append(consumers.Consumer(n, criticality="medium", uses=[
            consumers.Use(u["endpoint"], u.get("reads"), u.get("sends")) for u in entry["uses"]]))
    return consumers.assess(chs, found)


# ---- detection ------------------------------------------------------------------------------------

@pytest.mark.parametrize("fmt,file", [("swagger", "petstore.json"), ("graphql", "schema.graphql"), ("protobuf", "orders.proto"),
                                      ("asyncapi", "events.yaml"), ("wsdl", "orders.wsdl")])
def test_detect(fmt, file):
    folder = {"protobuf": "grpc", "wsdl": "soap"}.get(fmt, fmt)
    assert detect((FORMATS / folder / file).read_text("utf8"), file) == fmt


def test_unsupported_inputs():
    with pytest.raises(SpecError, match="unrecognised contract"):
        load_text("just some text", "notes.txt")
    with pytest.raises(SpecError, match="WSDL 2.0"):
        load_text('<description xmlns="http://www.w3.org/ns/wsdl"/>', "x.wsdl")
    with pytest.raises(SpecError, match="not a valid GraphQL schema"):
        load_text("type Query { a: Missing }", "s.graphql")


# ---- Swagger 2.0 ----------------------------------------------------------------------------------

def test_swagger2_converted_and_compared():
    head, chs = changes("swagger", "petstore.v1.json", "petstore.json")
    assert head.format == "swagger" and head.find("GET /api/pets/{x}") is not None  # basePath applied, param renamed
    assert breaking(chs) >= {
        ("type_changed", "/api/pets/{id}", "id"),                 # $ref into definitions resolved
        ("field_removed", "/api/pets/{id}", "owner"),
        ("required_field_added", "/api/pets", "species"),          # body parameter became a request body
        ("parameter_became_required", "/api/pets", "query:limit"),
    }


# ---- GraphQL --------------------------------------------------------------------------------------

def test_graphql_rules():
    head, chs = changes("graphql", "schema.v1.graphql", "schema.graphql")
    assert head.format == "graphql" and head.strict_inputs
    b = breaking(chs)
    assert ("field_removed", "cancelOrder", "reason") in b               # removing even an optional argument breaks
    assert ("required_field_added", "createOrder", "input.channel") in b
    assert ("type_changed", "order", "total") in b
    assert ("field_no_longer_guaranteed", "order", "customer.email") in b  # String! -> String
    nulls = [c for c in chs if c.endpoint == "QUERY order" and c.field == "customer.email"]
    assert len(nulls) == 1                                                 # not double-reported
    assert not any(c.breaking for c in chs if c.rule == "enum_value_added")


def test_graphql_discovery_reads_the_real_queries():
    head, chs = changes("graphql", "schema.v1.graphql", "schema.graphql")
    hits = consumers.discover_usage(FORMATS / "graphql" / "consumers" / "web", head)
    assert [(h["endpoint"], sorted(h["reads"])) for h in hits] == [("QUERY order", ["currency", "id", "status", "total"])]
    imp = impact_from_discovery("graphql", head, chs, ["web", "mobile"])
    got = {b["consumer"]: sorted(c["field"] for c in b["changes"]) for b in imp["consumers_broken"]}
    assert got == {"web": ["total"], "mobile": ["customer.email", "reason"]}


def test_graphql_introspection_json_loads():
    from graphql import build_schema, graphql_sync, get_introspection_query

    sdl = (FORMATS / "graphql" / "schema.graphql").read_text("utf8")
    data = graphql_sync(build_schema(sdl), get_introspection_query()).data
    spec = load_text(json.dumps({"data": data}), "introspection.json")
    assert spec.format == "graphql" and spec.find("QUERY order") is not None
    assert diff_specs(load_spec(FORMATS / "graphql", "schema.graphql"), spec) == []


# ---- protobuf -------------------------------------------------------------------------------------

def test_protobuf_wire_rules_and_imports():
    head, chs = changes("grpc", "orders.v1.proto", "orders.proto")
    b = breaking(chs)
    svc = "shop.v1.OrderService/"
    assert ("endpoint_removed", svc + "CancelOrder", "") in b
    assert ("field_number_changed", svc + "GetOrder", "items") in b
    assert ("type_changed", svc + "GetOrder", "items[].qty") in b
    assert ("message_replaced", svc + "ListOrders", "(message)") in b
    assert ("signature_changed", svc + "ListOrders", "server streaming") in b
    renamed = next(c for c in chs if c.rule == "field_renamed")
    assert (renamed.field, renamed.after, renamed.severity) == ("total", "grand_total", "medium")
    removed = next(c for c in chs if c.rule == "field_removed" and c.endpoint.endswith("GetOrder"))
    assert "not reserved" not in removed.description               # 4 was reserved
    order = head.find("RPC shop.v1.OrderService/GetOrder")
    money = order.responses["200"]["content"]["application/json"]["schema"]["properties"]["grand_total"]
    assert set(money["properties"]) == {"currency", "units"}       # Money came from common.proto via import


def test_protobuf_unreserved_number_is_flagged():
    v1 = 'syntax = "proto3"; package p; service S { rpc Get(R) returns (M); } message R {} message M { string a = 1; string b = 2; }'
    v2 = 'syntax = "proto3"; package p; service S { rpc Get(R) returns (M); } message R {} message M { string a = 1; }'
    chs = diff_specs(load_text(v1, "a.proto"), load_text(v2, "a.proto"))
    assert "field number 2 is not reserved" in chs[0].description


def test_protobuf_discovery_finds_stub_calls():
    head, chs = changes("grpc", "orders.v1.proto", "orders.proto")
    hits = consumers.discover_usage(FORMATS / "grpc" / "consumers" / "billing", head)
    assert [(h["endpoint"], h["file"]) for h in hits] == [("RPC shop.v1.OrderService/GetOrder", "invoice.py")]


# ---- AsyncAPI -------------------------------------------------------------------------------------

def test_asyncapi_2_to_3():
    head, chs = changes("asyncapi", "events.v1.yaml", "events.yaml")
    assert head.format == "asyncapi"
    assert breaking(chs) == {("field_removed", "order.created", "customerEmail"),
                             ("type_changed", "order.created", "total"),
                             ("required_field_added", "order.cancel", "requestedBy")}
    imp = impact_from_discovery("asyncapi", head, chs, ["mailer"])
    assert imp["verdict"] == "breaking"
    assert {c["field"] for c in imp["consumers_broken"][0]["changes"]} == {"customerEmail", "total", "requestedBy"}


# ---- SOAP / WSDL ----------------------------------------------------------------------------------

def test_wsdl_rules_and_discovery():
    head, chs = changes("soap", "orders.v1.wsdl", "orders.wsdl")
    assert head.format == "wsdl" and head.title == "OrderService"
    assert breaking(chs) == {("field_became_required", "OrderPort/CancelOrder", "Reason"),
                             ("signature_changed", "OrderPort/CancelOrder", "SOAPAction"),
                             ("field_removed", "OrderPort/GetOrder", "Order.CustomerEmail"),
                             ("type_changed", "OrderPort/GetOrder", "Order.Total")}
    hits = consumers.discover_usage(FORMATS / "soap" / "consumers" / "erp", head)
    assert [h["endpoint"] for h in hits] == ["SOAP OrderPort/GetOrder"]


# ---- deployed-contract drift ----------------------------------------------------------------------

class _Server:
    def __init__(self, handler):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _serve(get_body=None, post=None):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            self._send(get_body.encode(), "application/xml")

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(length))
            self._send(json.dumps(post(req)).encode(), "application/json")
    return _Server(H)


def test_graphql_drift_by_introspection():
    from graphql import build_schema, graphql_sync

    live = build_schema((FORMATS / "graphql" / "schema.graphql").read_text("utf8"))
    srv = _serve(post=lambda req: {"data": graphql_sync(live, req["query"]).data})
    try:
        res = drift.check(load_spec(FORMATS / "graphql", "schema.v1.graphql"), srv.url + "/graphql")
        assert res["verdict"] == "incompatible" and res["summary"]["breaking"] == 10
        same = drift.check(load_spec(FORMATS / "graphql", "schema.graphql"), srv.url + "/graphql")
        assert same["verdict"] == "matches"
    finally:
        srv.close()


def test_wsdl_and_openapi_drift():
    srv = _serve(get_body=(FORMATS / "soap" / "orders.wsdl").read_text("utf8"))
    try:
        assert drift.check(load_spec(FORMATS / "soap", "orders.v1.wsdl"), srv.url + "/orders?wsdl")["verdict"] == "incompatible"
        assert drift.check(load_spec(FORMATS / "soap", "orders.wsdl"), srv.url + "/orders?wsdl")["verdict"] == "matches"
    finally:
        srv.close()
    with pytest.raises(ValueError, match="reflection"):
        drift.check(load_spec(FORMATS / "grpc", "orders.proto"), "http://127.0.0.1:1")


def test_contract_ci_with_proto_imports_from_git(tmp_path, monkeypatch):
    proj = tmp_path / "svc"
    (proj / "proto").mkdir(parents=True)
    for f in ("common.proto",):
        (proj / "proto" / f).write_text((FORMATS / "grpc" / f).read_text("utf8"), "utf8")
    (proj / "proto" / "orders.proto").write_text((FORMATS / "grpc" / "orders.v1.proto").read_text("utf8"), "utf8")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(git + ["init", "-q", "-b", "main"], cwd=proj, check=True)
    subprocess.run(git + ["add", "."], cwd=proj, check=True)
    subprocess.run(git + ["commit", "-q", "-m", "v1"], cwd=proj, check=True)
    (proj / "proto" / "orders.proto").write_text((FORMATS / "grpc" / "orders.proto").read_text("utf8"), "utf8")
    monkeypatch.setenv("QA_MCP_ROOT", str(proj))
    assert cli(["contract-ci", "--spec", "proto/orders.proto", "--base", "HEAD", "--fail-on", "any-breaking"]) == 1
    report = json.loads((proj / ".qa-mcp/contract-report/contract-report.json").read_text("utf8"))
    assert report["summary"]["breaking"] == 7
    assert report["impact"]["verdict"] == "breaking_no_known_consumers"
