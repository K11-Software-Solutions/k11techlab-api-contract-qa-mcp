---
description: Generate and run contract tests for API endpoints, and check a running service against the spec
argument-hint: <spec path> [base URL of a running service] [endpoints, e.g. "GET /orders/{id}"]
---

Create contract tests for the API below, using the api-contract-qa MCP tools.

Input: $ARGUMENTS

1. Call `list_operations` on the spec and pick the endpoints asked for (or all of them, if there are few).
2. If a base URL is given, call `check_live_conformance` for each read-only (GET) endpoint first. Never call endpoints that change data (POST, PUT, PATCH, DELETE) on a shared environment without asking. Report any violations with the field path; they may be real bugs.
3. Call `generate_api_tests` with the framework the project uses: `pytest` for Python projects, `rest-assured` for Java (Maven or Gradle). Replace example values with realistic test data where the spec gives hints.
4. Save the files with `write_test` (ask before overwriting) and tell the user which dependencies to add.
5. If a base URL was given and it is a Python project, run the new tests and report the results.

Put tokens in headers as `${ENV:NAME}`, never literally.
