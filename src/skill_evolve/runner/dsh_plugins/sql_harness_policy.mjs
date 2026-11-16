const POLICY_VERSION = "dsh-sql-harness-policy-v1";
const ALLOWED_TOOLS = Object.freeze(["read", "grep", "sql_probe", "sql_submit"]);
const SQL_TOOL_NAMES = new Set(["sql_probe", "sql_submit"]);
const MAX_QUERY_CHARS = 65536;
const MAX_ERROR_CHARS = 1000;

export const name = "skill-evolve-sql-harness-policy";
export const inject = ["tools"];

function canonicalize(value) {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, canonicalize(value[key])]),
    );
  }
  return value;
}

function callKey(exec) {
  return JSON.stringify([exec.name, canonicalize(exec.arguments)]);
}

function normalizedResourcePath(value) {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.includes("\\") ||
    value.startsWith("/") ||
    /^[A-Za-z]:/.test(value)
  ) {
    return undefined;
  }
  const parts = value.split("/").filter((part) => part !== "." && part !== "");
  if (parts.length === 0 || parts.includes("..")) return undefined;
  return parts.join("/");
}

function resourceViolation(resourcePaths, exec) {
  if (exec.name === "read") {
    const path = normalizedResourcePath(exec.arguments?.file_path);
    if (path === undefined || !resourcePaths.includes(path)) {
      return "RESOURCE_NOT_ALLOWED: read only an exact path listed in TASK.md";
    }
  }
  if (exec.name === "grep") {
    const path = normalizedResourcePath(exec.arguments?.path);
    if (path === undefined || !resourcePaths.includes(path)) {
      return "RESOURCE_NOT_ALLOWED: grep requires an exact file path listed in TASK.md";
    }
  }
  return undefined;
}

function validateEndpoint(value) {
  if (typeof value !== "string") throw new Error("sql harness policy requires queryEndpoint");
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error("sql harness policy queryEndpoint is invalid");
  }
  const loopback = parsed.hostname === "127.0.0.1" || parsed.hostname === "[::1]";
  if (
    parsed.protocol !== "http:" ||
    !loopback ||
    parsed.username !== "" ||
    parsed.password !== "" ||
    parsed.pathname !== "/query" ||
    parsed.search !== "" ||
    parsed.hash !== ""
  ) {
    throw new Error("sql harness policy queryEndpoint must be a loopback /query URL");
  }
  return parsed.href;
}

function validateResources(value) {
  if (!Array.isArray(value)) throw new Error("sql harness policy requires resourcePaths");
  const seen = new Set();
  for (const path of value) {
    if (
      typeof path !== "string" ||
      path.length === 0 ||
      path.includes("\\") ||
      path.startsWith("/") ||
      /^[A-Za-z]:/.test(path) ||
      path.split("/").includes("..")
    ) {
      throw new Error("sql harness policy resourcePaths must be safe relative POSIX paths");
    }
    if (seen.has(path)) throw new Error("sql harness policy resourcePaths must be unique");
    seen.add(path);
  }
  return Object.freeze([...value]);
}

function validateConfig(config) {
  if (config === null || typeof config !== "object" || Array.isArray(config)) {
    throw new Error("sql harness policy config must be an object");
  }
  if (config.policyVersion !== POLICY_VERSION) {
    throw new Error("sql harness policy version is unsupported");
  }
  if (
    !Array.isArray(config.allowedTools) ||
    JSON.stringify(config.allowedTools) !== JSON.stringify(ALLOWED_TOOLS)
  ) {
    throw new Error("sql harness policy allowedTools does not match the fixed policy");
  }
  return Object.freeze({
    queryEndpoint: validateEndpoint(config.queryEndpoint),
    resourcePaths: validateResources(config.resourcePaths),
  });
}

function sanitizeDetail(value) {
  if (typeof value !== "string" || value.length === 0) return undefined;
  return value
    .replace(/https?:\/\/[^\s"']+/gi, "<redacted-url>")
    .replace(/[A-Za-z]:[\\/][^\s"']+/g, "<redacted-path>")
    .replace(/\/(?:Users|home|var|tmp)\/[^\s"']+/g, "<redacted-path>")
    .replace(/(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*[^\s,;]+/gi, "$1=<redacted>")
    .slice(0, MAX_ERROR_CHARS);
}

function queryArguments(value) {
  if (
    value === null ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    typeof value.query !== "string" ||
    value.query.trim().length === 0 ||
    value.query.length > MAX_QUERY_CHARS
  ) {
    throw new Error("SQL_QUERY_INVALID");
  }
  return value.query;
}

function validSuccessPayload(payload) {
  return (
    payload !== null &&
    typeof payload === "object" &&
    payload.ok === true &&
    Array.isArray(payload.columns) &&
    payload.columns.every((column) => typeof column === "string") &&
    Array.isArray(payload.rows)
  );
}

async function executeQuery(endpoint, intent, args, exec) {
  const query = queryArguments(args);
  let response;
  try {
    response = await fetch(endpoint, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ query }),
      signal: exec.signal,
    });
  } catch {
    throw new Error("SQL_QUERY_SERVICE_UNAVAILABLE");
  }

  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error("SQL_QUERY_RESPONSE_INVALID");
  }
  if (validSuccessPayload(payload) && response.ok) {
    return {
      ok: true,
      intent,
      phase: intent === "submit" ? "finalize" : "explore",
      columns: payload.columns,
      rows: payload.rows,
      ...(intent === "submit"
        ? { guidance: "Final candidate executed successfully. Emit that exact SQL in one sql fenced block without calling another tool." }
        : {}),
    };
  }
  if (payload !== null && typeof payload === "object" && payload.ok === false) {
    const detail = sanitizeDetail(payload.error);
    return {
      ok: false,
      intent,
      phase: "explore",
      error_code: payload.unsafe === true ? "SQL_QUERY_UNSAFE" : "SQL_QUERY_REJECTED",
      ...(detail === undefined ? {} : { detail }),
    };
  }
  throw new Error("SQL_QUERY_RESPONSE_INVALID");
}

function outputDefinition() {
  return {
    schema: {
      type: "object",
      additionalProperties: false,
      required: ["ok", "intent", "phase"],
      properties: {
        ok: { type: "boolean" },
        intent: { type: "string", enum: ["probe", "submit"] },
        phase: { type: "string", enum: ["explore", "finalize"] },
        columns: { type: "array", items: { type: "string" } },
        rows: { type: "array" },
        guidance: { type: "string" },
        error_code: { type: "string" },
        detail: { type: "string" },
      },
    },
    render(_args, value) {
      return [{ type: "text", text: JSON.stringify(value) }];
    },
  };
}

function toolDefinition(endpoint, intent) {
  const toolName = intent === "submit" ? "sql_submit" : "sql_probe";
  return {
    name: toolName,
    description:
      intent === "submit"
        ? "Execute the final read-only PostgreSQL candidate. On success, emit that exact query as the final answer without more tool calls."
        : "Execute one read-only PostgreSQL query to inspect the disposable evaluation fixture.",
    parameters: {
      type: "object",
      additionalProperties: false,
      required: ["query"],
      properties: {
        query: {
          type: "string",
          description: "One SELECT, WITH, or EXPLAIN statement.",
        },
      },
    },
    output: outputDefinition(),
    async execute(args, exec) {
      return executeQuery(endpoint, intent, args, exec);
    },
  };
}

function acceptedSuccessfulCall(exec, result, decision) {
  if (decision.kind !== "accept" || result.isError) return false;
  if (!SQL_TOOL_NAMES.has(exec.name)) return true;
  return result.value !== null && typeof result.value === "object" && result.value.ok === true;
}

export function apply(ctx, rawConfig) {
  const config = validateConfig(rawConfig);
  ctx.tools.register(toolDefinition(config.queryEndpoint, "probe"));
  ctx.tools.register(toolDefinition(config.queryEndpoint, "submit"));

  ctx.on("agent/created", ({ agent }) => {
    const state = { phase: "EXPLORE", successfulCalls: new Set() };
    agent.ctx.tools.restrict({ allow: ALLOWED_TOOLS });
    agent.ctx.tools.guard((exec) => {
      if (!ALLOWED_TOOLS.includes(exec.name)) {
        return "TOOL_NOT_ALLOWED: this SQL task exposes only read, grep, sql_probe, and sql_submit";
      }
      if (state.phase === "FINALIZE") {
        return "SQL_FINALIZED: emit the successfully submitted SQL as the final answer; no more tools are allowed";
      }
      const resourceError = resourceViolation(config.resourcePaths, exec);
      if (resourceError !== undefined) return resourceError;
      if (state.successfulCalls.has(callKey(exec))) {
        return "TOOL_CALL_ALREADY_SUCCEEDED: reuse the existing observation instead of repeating it";
      }
      return undefined;
    });
    agent.ctx.on("tools/post-execute", async (exec, result, next) => {
      const decision = await next();
      if (exec.agent !== agent || !acceptedSuccessfulCall(exec, result, decision)) return decision;
      state.successfulCalls.add(callKey(exec));
      if (exec.name === "sql_submit") state.phase = "FINALIZE";
      return decision;
    });
  });
}

export const __test = Object.freeze({
  ALLOWED_TOOLS,
  POLICY_VERSION,
  callKey,
  normalizedResourcePath,
  resourceViolation,
  sanitizeDetail,
  validateConfig,
});
