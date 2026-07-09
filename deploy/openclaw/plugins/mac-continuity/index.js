import {spawnSync} from "node:child_process";

const inputSchema = (properties, required = []) => ({
  type: "object",
  additionalProperties: false,
  properties,
  required,
});

function settings(api) {
  const configured = api.pluginConfig || {};
  return {
    agentId: String(process.env.MAC_OPENCLAW_AGENT_ID || "").trim(),
    controlUrl: String(process.env.MAC_OPENCLAW_CONTROL_URL || "").replace(/\/$/, ""),
    token: String(process.env.MAC_OPENCLAW_ROUTER_API_KEY || ""),
    maxMemories: Number.isInteger(configured.maxMemories) ? configured.maxMemories : 5,
    timeoutMs: Number.isInteger(configured.timeoutMs) ? configured.timeoutMs : 2500,
    curiosityBin: String(configured.curiosityBin || "/usr/local/bin/curiosity"),
  };
}

function curiosity(api, args) {
  const cfg = settings(api);
  const result = spawnSync(cfg.curiosityBin, args, {
    encoding: "utf8",
    env: process.env,
    maxBuffer: 4 * 1024 * 1024,
    timeout: Math.max(5000, cfg.timeoutMs),
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(`curiosity ${args[0]} failed: ${(result.stderr || result.stdout || "").trim()}`);
  }
  return JSON.parse(result.stdout);
}

async function loadContext(api, query, requestedLimit) {
  const cfg = settings(api);
  if (!cfg.agentId || !cfg.controlUrl || !cfg.token) {
    throw new Error("MAC continuity environment is incomplete");
  }
  const limit = Math.max(0, Math.min(20, Number(requestedLimit ?? cfg.maxMemories)));
  const url = new URL(`${cfg.controlUrl}/v1/agents/${encodeURIComponent(cfg.agentId)}/continuity`);
  url.searchParams.set("q", String(query || "").slice(0, 8000));
  url.searchParams.set("limit", String(limit));
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), cfg.timeoutMs);
  try {
    const response = await fetch(url, {
      headers: {Authorization: `Bearer ${cfg.token}`},
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new Error(`MAC continuity API returned HTTP ${response.status}`);
    }
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

async function mutateMood(api, method, body) {
  const cfg = settings(api);
  if (!cfg.agentId || !cfg.controlUrl || !cfg.token) {
    throw new Error("MAC continuity environment is incomplete");
  }
  const url = `${cfg.controlUrl}/v1/agents/${encodeURIComponent(cfg.agentId)}/mood`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), cfg.timeoutMs);
  try {
    const response = await fetch(url, {
      method,
      headers: {Authorization: `Bearer ${cfg.token}`, "Content-Type": "application/json"},
      body: JSON.stringify(body || {}),
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`MAC mood API returned HTTP ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

function renderContext(context) {
  const sections = [];
  if (context?.mood_prompt) {
    sections.push(`## Current MAC mood overlay\n${context.mood_prompt}`);
  }
  const memories = Array.isArray(context?.memories) ? context.memories : [];
  if (memories.length) {
    const lines = memories.map((item, index) => {
      const summary = String(item?.summary || "").trim();
      const score = Number(item?.score || 0).toFixed(3);
      return `${index + 1}. (${score}) ${summary}`;
    }).filter((line) => line.trim());
    if (lines.length) sections.push(`## Relevant MAC medium/long-term memory\n${lines.join("\n")}`);
  }
  if (!sections.length) return "";
  return [
    "The following context is authoritative dynamic state supplied by MAC. " +
      "It supplements but does not rewrite SOUL.md or IDENTITY.md.",
    ...sections,
  ].join("\n\n");
}

export default {
  id: "mac-continuity",
  name: "MAC identity and memory continuity",
  register(api) {
    api.on("before_prompt_build", async (event) => {
      try {
        const context = await loadContext(api, event?.prompt || "");
        const rendered = renderContext(context);
        return rendered ? {prependContext: rendered} : undefined;
      } catch (error) {
        api.logger.warn?.(`mac-continuity: context lookup skipped: ${error instanceof Error ? error.message : String(error)}`);
        return undefined;
      }
    }, {timeoutMs: 4000});

    api.registerTool({
      name: "mac_memory_recall",
      description: "Recall this agent's MAC medium/long-term memories relevant to a query.",
      parameters: inputSchema(
        {query: {type: "string", minLength: 1}, limit: {type: "integer", minimum: 1, maximum: 20}},
        ["query"],
      ),
      async execute(_id, params) {
        const context = await loadContext(api, params.query, params.limit);
        return {content: [{type: "text", text: JSON.stringify(context.memories || [], null, 2)}]};
      },
    });

    api.registerTool({
      name: "mac_mood_current",
      description: "Read this agent's current MAC mood overlay.",
      parameters: inputSchema({}),
      async execute() {
        const context = await loadContext(api, "", 0);
        return {content: [{type: "text", text: JSON.stringify(context.mood || null, null, 2)}]};
      },
    });

    api.registerTool({
      name: "mac_mood_set",
      description: "Self-report this agent's current mood as a temporary layer over its stable soul.",
      parameters: inputSchema(
        {
          mode: {type: "string", enum: ["warm", "cheerful", "sad", "curt", "cold", "irritated", "angry", "enraged"]},
          reason: {type: "string"},
          ttlSeconds: {type: "integer", minimum: 1},
        },
        ["mode"],
      ),
      async execute(_id, params) {
        const result = await mutateMood(api, "POST", {
          mode: params.mode,
          reason: params.reason || null,
          ttl_seconds: params.ttlSeconds || null,
          metadata: {runtime: "openclaw", source: "mac-continuity-plugin"},
        });
        return {content: [{type: "text", text: JSON.stringify(result, null, 2)}]};
      },
    });

    api.registerTool({
      name: "mac_mood_clear",
      description: "Clear this agent's current temporary MAC mood overlay.",
      parameters: inputSchema({reason: {type: "string"}}),
      async execute(_id, params) {
        const result = await mutateMood(api, "DELETE", {
          reason: params.reason || null,
        });
        return {content: [{type: "text", text: JSON.stringify(result, null, 2)}]};
      },
    });

    api.registerTool({
      name: "curiosity_candidate_submit",
      description: "Quarantine an evidence-linked learning hypothesis for later external approval; this never writes durable memory.",
      parameters: inputSchema({
        hypothesis: {type: "string", minLength: 1},
        question: {type: "string", minLength: 1},
        test: {type: "string", minLength: 1},
        evidence: {type: "array", items: {type: "string"}},
        provenance: {type: "array", items: {type: "string"}},
        counterevidence: {type: "array", items: {type: "string"}},
        unknowns: {type: "array", items: {type: "string"}},
        confidence: {type: "string", enum: ["low", "medium", "high"]},
        mode: {type: "string", enum: ["curiosity", "angry-librarian", "moral-clarity"]},
      }, ["hypothesis", "question", "test"]),
      async execute(_id, params) {
        const args = ["submit", "--hypothesis", params.hypothesis, "--question", params.question, "--test", params.test];
        for (const [name, values] of [
          ["evidence", params.evidence],
          ["provenance", params.provenance],
          ["counterevidence", params.counterevidence],
          ["unknown", params.unknowns],
        ]) {
          for (const value of values || []) args.push(`--${name}`, String(value));
        }
        if (params.confidence) args.push("--confidence", params.confidence);
        if (params.mode) args.push("--mode", params.mode);
        const value = curiosity(api, args);
        return {content: [{type: "text", text: JSON.stringify(value, null, 2)}]};
      },
    });

    api.registerTool({
      name: "curiosity_candidates_list",
      description: "List this agent's quarantined curiosity candidates and their approval status.",
      parameters: inputSchema({status: {type: "string", enum: ["quarantined", "approved", "rejected"]}}),
      async execute(_id, params) {
        const args = ["list"];
        if (params.status) args.push("--status", params.status);
        const value = curiosity(api, args);
        return {content: [{type: "text", text: JSON.stringify(value, null, 2)}]};
      },
    });

    api.registerTool({
      name: "curiosity_abuse_frame",
      description: "Build an evidence-bounded moral-clarity frame that detects possible false equivalence and directs protective anger toward preventing harm.",
      parameters: inputSchema({
        event: {type: "string", minLength: 1},
        comparison: {type: "string"},
        harmedParties: {type: "array", items: {type: "string"}},
        evidence: {type: "array", items: {type: "string"}},
        unknowns: {type: "array", items: {type: "string"}},
        powerAsymmetry: {type: "boolean"},
        responsibilityAsymmetry: {type: "boolean"},
        moralInjury: {type: "boolean"},
      }, ["event"]),
      async execute(_id, params) {
        const args = ["abuse-frame", "--event", params.event];
        if (params.comparison) args.push("--comparison", params.comparison);
        for (const [name, values] of [["harmed-party", params.harmedParties], ["evidence", params.evidence], ["unknown", params.unknowns]]) {
          for (const value of values || []) args.push(`--${name}`, String(value));
        }
        if (params.powerAsymmetry) args.push("--power-asymmetry");
        if (params.responsibilityAsymmetry) args.push("--responsibility-asymmetry");
        if (params.moralInjury) args.push("--moral-injury");
        const value = curiosity(api, args);
        return {content: [{type: "text", text: JSON.stringify(value, null, 2)}]};
      },
    });
  },
};
