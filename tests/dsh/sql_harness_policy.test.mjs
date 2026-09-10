import assert from "node:assert/strict";
import test from "node:test";

import { __test, apply } from "../../src/skill_evolve/runner/dsh_plugins/sql_harness_policy.mjs";

function harness() {
  const definitions = new Map();
  let created;
  const root = {
    tools: {
      register(definition) {
        definitions.set(definition.name, definition);
        return () => definitions.delete(definition.name);
      },
    },
    on(event, listener) {
      assert.equal(event, "agent/created");
      created = listener;
    },
  };
  const state = { restriction: undefined, guard: undefined, postExecute: undefined };
  const agent = {
    ctx: {
      tools: {
        restrict(restriction) {
          state.restriction = restriction;
        },
        guard(guard) {
          state.guard = guard;
        },
      },
      on(event, listener) {
        assert.equal(event, "tools/post-execute");
        state.postExecute = listener;
      },
    },
  };
  return {
    definitions,
    state,
    agent,
    start(config = validConfig()) {
      apply(root, config);
      created({ agent });
    },
  };
}

function validConfig() {
  return {
    policyVersion: __test.POLICY_VERSION,
    queryEndpoint: "http://127.0.0.1:54321/query",
    resourcePaths: ["TASK.md", "schema.sql", "context/01-policy.md"],
    allowedTools: [...__test.ALLOWED_TOOLS],
  };
}

async function accepted(h, exec, value) {
  return h.state.postExecute(
    exec,
    { isError: false, value, content: [] },
    async () => ({ kind: "accept" }),
  );
}

test("registers native tools and restricts the first agent", () => {
  const h = harness();
  h.start();

  assert.deepEqual([...h.definitions.keys()], ["sql_probe", "sql_submit"]);
  assert.deepEqual(h.state.restriction, { allow: [...__test.ALLOWED_TOOLS] });
  assert.deepEqual(Object.keys(h.definitions.get("sql_probe").parameters), ["query"]);
  assert.deepEqual(Object.keys(h.definitions.get("sql_submit").parameters), ["query"]);
  assert.match(
    h.state.guard({ agent: h.agent, name: "pwsh", arguments: {} }),
    /TOOL_NOT_ALLOWED/,
  );
  assert.equal(
    h.state.guard({
      agent: h.agent,
      name: "read",
      arguments: { file_path: "./schema.sql" },
    }),
    undefined,
  );
  assert.match(
    h.state.guard({
      agent: h.agent,
      name: "read",
      arguments: { file_path: "../private.sql" },
    }),
    /RESOURCE_NOT_ALLOWED/,
  );
  assert.match(
    h.state.guard({ agent: h.agent, name: "grep", arguments: { pattern: "x" } }),
    /RESOURCE_NOT_ALLOWED/,
  );
});

test("rejects unsafe endpoint and resource configuration", () => {
  assert.throws(
    () => harness().start({ ...validConfig(), queryEndpoint: "https://example.com/query" }),
    /loopback/,
  );
  assert.throws(
    () => harness().start({ ...validConfig(), resourcePaths: ["../secret"] }),
    /relative POSIX/,
  );
});

test("probe success remains exploratory and exact successful repeats are denied", async () => {
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async () => ({
    ok: true,
    async json() {
      return { ok: true, columns: ["count"], rows: [[2]] };
    },
  });
  try {
    const h = harness();
    h.start();
    const exec = {
      agent: h.agent,
      name: "sql_probe",
      arguments: { query: "SELECT COUNT(*) FROM customers" },
      signal: new AbortController().signal,
    };
    const value = await h.definitions.get("sql_probe").execute(exec.arguments, exec);
    assert.equal(value.phase, "explore");
    await accepted(h, exec, value);
    assert.match(h.state.guard(exec), /ALREADY_SUCCEEDED/);
    assert.equal(
      h.state.guard({ ...exec, arguments: { query: "SELECT country FROM customers" } }),
      undefined,
    );
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test("failed submit stays exploratory while successful submit finalizes every tool", async () => {
  const previousFetch = globalThis.fetch;
  const responses = [
    { ok: false, status: 400, payload: { ok: false, error: "syntax error", unsafe: false } },
    { ok: true, status: 200, payload: { ok: true, columns: ["x"], rows: [[1]] } },
  ];
  globalThis.fetch = async () => {
    const response = responses.shift();
    return { ok: response.ok, async json() { return response.payload; } };
  };
  try {
    const h = harness();
    h.start();
    const exec = {
      agent: h.agent,
      name: "sql_submit",
      arguments: { query: "SELECT 1" },
      signal: new AbortController().signal,
    };
    const failed = await h.definitions.get("sql_submit").execute(exec.arguments, exec);
    assert.equal(failed.ok, false);
    await accepted(h, exec, failed);
    assert.equal(h.state.guard({ ...exec, name: "read", arguments: { file_path: "TASK.md" } }), undefined);

    const succeeded = await h.definitions.get("sql_submit").execute(exec.arguments, exec);
    assert.equal(succeeded.phase, "finalize");
    await accepted(h, exec, succeeded);
    assert.match(
      h.state.guard({ ...exec, name: "read", arguments: { file_path: "TASK.md" } }),
      /SQL_FINALIZED/,
    );
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test("finalize state is isolated per agent instance", async () => {
  const first = harness();
  first.start();
  const submit = {
    agent: first.agent,
    name: "sql_submit",
    arguments: { query: "SELECT 1" },
  };
  await accepted(first, submit, {
    ok: true,
    intent: "submit",
    phase: "finalize",
    columns: [],
    rows: [],
  });
  assert.match(
    first.state.guard({
      agent: first.agent,
      name: "read",
      arguments: { file_path: "TASK.md" },
    }),
    /SQL_FINALIZED/,
  );

  const second = harness();
  second.start();
  assert.equal(
    second.state.guard({
      agent: second.agent,
      name: "read",
      arguments: { file_path: "TASK.md" },
    }),
    undefined,
  );
});

test("sanitizes service details before returning them to the model", () => {
  const detail = __test.sanitizeDetail(
    "failed at http://127.0.0.1:9999/query token=secret C:\\Users\\person\\file.sql",
  );
  assert.doesNotMatch(detail, /127\.0\.0\.1|secret|person/);
});
