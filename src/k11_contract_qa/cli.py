"""Command line: start the MCP server (default) or run contract-ci."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__

USAGE = f"""k11-api-contract-qa-mcp {__version__}

Usage:
  k11-api-contract-qa-mcp                         start the MCP server on stdio
  k11-api-contract-qa-mcp contract-ci [options]   check an API change and write an audit report
  k11-api-contract-qa-mcp --version

contract-ci options:
  --spec PATH          the OpenAPI file in this branch (required)
  --base REF           git ref to compare with (default origin/main)
  --base-spec SPEC     compare with this file or <ref>:<path> instead of --base
  --consumers PATH     default .qa-mcp/consumers.yaml
  --fail-on LEVEL      breaking (default) | needs-human | any-breaking | never
  --report-dir PATH    default .qa-mcp/contract-report

The project is QA_MCP_ROOT or the current directory.
"""


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        from .server import serve

        serve()
        return 0
    if argv[0] in ("--version", "-v"):
        print(__version__)
        return 0
    if argv[0] in ("--help", "-h", "help"):
        print(USAGE)
        return 0
    if argv[0] != "contract-ci":
        print(f"Unknown command: {argv[0]}\n\n{USAGE}", file=sys.stderr)
        return 2

    p = argparse.ArgumentParser(prog="k11-api-contract-qa-mcp contract-ci")
    p.add_argument("--spec", required=True)
    p.add_argument("--base", default="origin/main")
    p.add_argument("--base-spec")
    p.add_argument("--consumers")
    p.add_argument("--fail-on", default="breaking", choices=["breaking", "needs-human", "any-breaking", "never"])
    p.add_argument("--report-dir", default=".qa-mcp/contract-report")
    a = p.parse_args(argv[1:])

    from . import consumers, report
    from .diff import diff_specs
    from .spec import SpecError, inside, load_spec

    root = Path(os.environ.get("QA_MCP_ROOT") or os.getcwd()).resolve()
    base_src = a.base_spec or f"{a.base}:{a.spec}"
    try:
        head = load_spec(root, a.spec)
        base = load_spec(root, base_src)
        changes = diff_specs(base, head)
        cons = consumers.load_consumers(inside(root, a.consumers or ".qa-mcp/consumers.yaml"))
        impact = consumers.assess(changes, cons)
        r = report.build(base_src, a.spec, head.title, changes, impact)
        md, js = report.write(r, inside(root, a.report_dir))
    except (SpecError, ValueError, OSError) as e:
        print(f"contract-ci failed: {e}", file=sys.stderr)
        return 1

    v = impact["verdict"]
    print(f"{v}: {r['summary']['breaking']} breaking change(s), {len(impact['consumers_broken'])} consumer(s) affected"
          f"{'; needs a person' if impact['needs_human'] else ''}. Report: {md}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf8") as fh:
            fh.write(f"verdict={v}\nbreaking={r['summary']['breaking']}\nconsumers_affected={len(impact['consumers_broken'])}\n"
                     f"needs_human={str(impact['needs_human']).lower()}\nreport={md}\nreport_json={js}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf8") as fh:
            fh.write(md.read_text("utf8") + "\n")
    fail = {"breaking": v == "breaking", "needs-human": impact["needs_human"],
            "any-breaking": r["summary"]["breaking"] > 0, "never": False}[a.fail_on]
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
