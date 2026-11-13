---
description: Check an API change against its consumers before merge, and explain who breaks and why
argument-hint: [contract path, default openapi.yaml] [base ref, default origin/main]
---

Check whether the API change in this branch breaks known consumers, using the api-contract-qa MCP tools.

Input: $ARGUMENTS (optional spec path and base git ref)

1. Find the contract (the argument, else `openapi.yaml` / `openapi.json` / `swagger.json` / `*.graphql` / `*.proto` / `asyncapi.yaml` / `*.wsdl`). Any of these formats works; it is detected automatically. Use `<base ref>:<path>` (default `origin/main`) as the base.
2. Call `assess_impact` with `write_report: true`.
3. If `.qa-mcp/consumers.yaml` is missing or lists no consumers, say so plainly: the verdict can't name who breaks. Offer to run `discover_consumer_usage` on consumer repos the user points to and draft the file.
4. Explain the result:
   - for each affected consumer: the change, why it affects them (the field they read or send), and the owner to notify;
   - breaking changes no known consumer uses (safe only if the consumer list is complete);
   - whether a person should decide before merge, and why.
5. Suggest the least disruptive fix for each breaking change: keep the old field alongside the new one, make a new request field optional with a default, version the endpoint, or deprecate before removing.

Never call a change safe just because no consumer is listed. Finish with the verdict in one line.
