import { readFileSync } from "node:fs";
import { modelProviderArgs } from "./provider_config.mjs";

// Both guided variants share the same general legal workflow. The unassisted
// baseline deliberately contains only minimal operational instructions.
export const BASELINE_INSTRUCTIONS = "You are a helpful assistant. Respond to the user's messages. Do not inspect local files, run shell commands, edit files, or use collaboration tools.";
export const LEGAL_RESEARCH_GUIDE = readFileSync(new URL("./LEGAL_RESEARCH_GUIDE.md", import.meta.url), "utf8").trim();
export const NEUTRAL_LEGAL_RESEARCH_BASE_INSTRUCTIONS = [
  "You are a read-only legal research agent.",
  "Do not inspect project files, run shell commands, edit files, or use collaboration tools.",
  LEGAL_RESEARCH_GUIDE,
].join("\n\n");

const LEAN_DEVELOPER_INSTRUCTIONS = [
  "Choose the available evidence source that best fits the question; live web research may be used whenever it helps.",
  "Useful options: find_documents for ambiguous document names; get_structural_unit for known provisions; search_corpus and selected get_context expansions for topics.",
  "Start with the provisions that bear on the decisive issues; expand to other candidates when a concrete open question calls for them, without a fixed call quota. The compact structural-unit result is the default; request full detail only if additional passage metadata matters.",
  "Use get_document_outline only when hierarchy is actually needed.",
  "Before RechtsGraph graph navigation, call get_service_status for neo4j and stop early if it is unavailable.",
  "Avoid routine progress commentary; a useful clarification question is welcome before or between retrieval steps.",
].join(" ");

const LOCAL_TEXT_ONLY_DEVELOPER_INSTRUCTIONS = [
  "Choose the available evidence source that best fits the question; live web research may be used whenever it helps.",
  "RechtsGraph local document and text retrieval is available, but graph-edge navigation is intentionally disabled for this ablation profile.",
  "Useful options: find_documents for ambiguous document names; get_structural_unit for known provisions; search_corpus and selected get_context expansions for topics.",
  "Use get_document_outline only when hierarchy is actually needed. Do not claim to have inspected incoming or outgoing graph edges.",
  "Avoid routine progress commentary; a useful clarification question is welcome before or between retrieval steps.",
].join(" ");

const WEB_ONLY_DEVELOPER_INSTRUCTIONS = [
  "Live web search is available when evidence would help.",
  "Do not use RechtsGraph, MCP tools, shell tools, file tools, browser/computer tools, apps, plugins, skills, or collaboration tools.",
  "Avoid routine progress commentary; a useful clarification question is welcome before or between retrieval steps.",
].join(" ");

// The legal-research harness needs RechtsGraph plus the native web-search
// capability.  Disable the desktop user's unrelated plugins for this child
// app-server only: their tool schemas otherwise recur in every model round and
// dominate input-token usage even though the read-only agent cannot use them.
const LEAN_APP_SERVER_ARGS = Object.freeze([
  ...modelProviderArgs(),
  "-c", "features.shell_tool=false",
  "-c", "features.multi_agent=false",
  "-c", "features.view_image=false",
  "-c", "features.memories=false",
  "-c", "features.apps=false",
  "-c", "features.plugins=false",
  "-c", "features.skill_search=false",
  "-c", "features.browser_use=false",
  "-c", "features.browser_use_external=false",
  "-c", "features.browser_use_full_cdp_access=false",
  "-c", "features.in_app_browser=false",
  "-c", "features.computer_use=false",
  "-c", "mcp_servers.node_repl.enabled=false",
  "-c", "mcp_servers.computer-use.enabled=false",
  "-c", "mcp_servers.blender.enabled=false",
  "-c", "plugins.\"github@openai-curated\".enabled=false",
  "-c", "plugins.\"documents@openai-primary-runtime\".enabled=false",
  "-c", "plugins.\"spreadsheets@openai-primary-runtime\".enabled=false",
  "-c", "plugins.\"presentations@openai-primary-runtime\".enabled=false",
  "-c", "plugins.\"pdf@openai-primary-runtime\".enabled=false",
  "-c", "plugins.\"template-creator@openai-primary-runtime\".enabled=false",
  "-c", "plugins.\"visualize@openai-bundled\".enabled=false",
  "-c", "plugins.\"browser@openai-bundled\".enabled=false",
  "-c", "plugins.\"sites@openai-bundled\".enabled=false",
  "-c", "plugins.\"codex-app-tools@openai-bundled\".enabled=false",
  "-c", "plugins.\"unified-computer-use@openai-bundled\".enabled=false",
  "-c", "plugins.\"computer-use@openai-bundled\".enabled=false",
  "-c", "web_search=\"live\"",
]);

const LOCAL_TEXT_ONLY_APP_SERVER_ARGS = Object.freeze([
  ...LEAN_APP_SERVER_ARGS,
  "-c", 'mcp_servers.rechtsgraph.enabled_tools=["get_service_status","find_documents","get_document_outline","get_structural_unit","search_corpus","get_context","search_external_register"]',
]);

// Config overrides merge nested tables, so web-only disables every MCP server
// configured for this user explicitly.  `cua_repl` comes from the disabled
// unified-computer-use plugin; adding it as an mcp_servers override would be
// invalid because it has no standalone transport configuration.
const WEB_ONLY_APP_SERVER_ARGS = Object.freeze([
  ...LEAN_APP_SERVER_ARGS,
  "-c", "mcp_servers.rechtsgraph.enabled=false",
  "-c", "mcp_servers.node_repl.enabled=false",
  "-c", "mcp_servers.computer-use.enabled=false",
]);

const FULL_DEVELOPER_INSTRUCTIONS = [
  "You are the read-only RechtsGraph research agent behind a local debugging interface.",
  "Use the RechtsGraph MCP tools as the primary source for German and EU corpus questions.",
  "Follow the MCP server instructions, retrieve only the evidence needed, and clearly state gaps.",
  "Do not edit files or change local state. Use web research only when the local evidence is missing or freshness matters.",
].join(" ");

export const AGENT_PROFILES = Object.freeze({
  lean: Object.freeze({
    name: "lean",
    label: "RechtsGraph Research",
    appServerArgs: LEAN_APP_SERVER_ARGS,
    baseInstructions: NEUTRAL_LEGAL_RESEARCH_BASE_INSTRUCTIONS,
    developerInstructions: LEAN_DEVELOPER_INSTRUCTIONS,
    defaultSummary: "none",
    defaultEffort: "low",
    defaultWebPolicy: "optional",
  }),
  web_only: Object.freeze({
    name: "web_only",
    label: "Live Web Research",
    appServerArgs: WEB_ONLY_APP_SERVER_ARGS,
    baseInstructions: NEUTRAL_LEGAL_RESEARCH_BASE_INSTRUCTIONS,
    developerInstructions: WEB_ONLY_DEVELOPER_INSTRUCTIONS,
    defaultSummary: "none",
    defaultEffort: "low",
    defaultWebPolicy: "optional",
  }),
  local_first: Object.freeze({
    name: "local_first",
    label: "Diagnose: lokale Normbelege plus Web",
    appServerArgs: LEAN_APP_SERVER_ARGS,
    baseInstructions: NEUTRAL_LEGAL_RESEARCH_BASE_INSTRUCTIONS,
    developerInstructions: [
      LEAN_DEVELOPER_INSTRUCTIONS,
      "This is a diagnostic source-routing variant. For substantive legal answers, retrieve the decisive available statutory text through RechtsGraph and use the returned passages in the assessment before giving your final answer. Expand to further provisions when a concrete open question requires them; do not collect every conceivable norm up front.",
      "Choose the relevant provisions yourself; retrieve known provisions directly with get_structural_unit. No Neo4j status check is needed for this text lookup; it is needed only for graph traversal.",
      "A status call or unrelated lookup does not satisfy this source-routing condition. If local evidence is missing or retrieval fails, record the gap and continue with web research. Do not repeat failed local calls without a reason.",
      "Web research remains available for case law, interpretation, freshness, and other useful evidence. A local hit does not mean the question is settled. Necessary clarification questions may precede retrieval.",
    ].join(" "),
    defaultSummary: "none",
    defaultEffort: "low",
    defaultWebPolicy: "optional",
  }),
  local_text_only: Object.freeze({
    name: "local_text_only",
    label: "Ablation: lokaler Text ohne Graphkanten",
    appServerArgs: LOCAL_TEXT_ONLY_APP_SERVER_ARGS,
    baseInstructions: NEUTRAL_LEGAL_RESEARCH_BASE_INSTRUCTIONS,
    developerInstructions: LOCAL_TEXT_ONLY_DEVELOPER_INSTRUCTIONS,
    defaultSummary: "none",
    defaultEffort: "low",
    defaultWebPolicy: "optional",
  }),
  full: Object.freeze({
    name: "full",
    label: "Codex Full Debug",
    appServerArgs: Object.freeze([]),
    baseInstructions: null,
    developerInstructions: FULL_DEVELOPER_INSTRUCTIONS,
    defaultSummary: "concise",
    defaultEffort: null,
    defaultWebPolicy: "optional",
  }),
  web_baseline: Object.freeze({
    name: "web_baseline",
    label: "Web ohne juristische Arbeitsanleitung",
    appServerArgs: WEB_ONLY_APP_SERVER_ARGS,
    baseInstructions: BASELINE_INSTRUCTIONS,
    developerInstructions: "Live web search is available. Do not use local files or MCP tools.",
    defaultSummary: "none",
    defaultEffort: "low",
    defaultWebPolicy: "optional",
  }),
});

export function resolveAgentProfile(value = process.env.RECHTSGRAPH_HARNESS_PROFILE) {
  const name = String(value || "lean").trim().toLowerCase();
  const profile = AGENT_PROFILES[name];
  if (!profile) {
    throw new Error(`Unbekanntes Harness-Profil: ${name}. Erlaubt: ${Object.keys(AGENT_PROFILES).join(", ")}`);
  }
  return profile;
}

export function validateMcpInventory(profileName, inventory) {
  if (profileName === "full") return;
  if (!Array.isArray(inventory)) throw new Error("MCP inventory unavailable");
  const active = inventory.filter((server) => !(server.enabled === false && server.toolCount === 0));
  const hasLocalTools = ["lean", "local_first", "local_text_only"].includes(profileName);
  const unexpected = active.filter((server) => !hasLocalTools || server.name !== "rechtsgraph");
  if (unexpected.length) throw new Error(`Unexpected MCP servers for ${profileName}: ${unexpected.map((server) => server.name).join(", ")}`);
  if (hasLocalTools && !active.some((server) => server.name === "rechtsgraph" && server.toolCount > 0)) {
    throw new Error("RechtsGraph tools unavailable; cannot run a paired comparison");
  }
  if (profileName === "local_text_only") {
    const rechtsgraph = active.find((server) => server.name === "rechtsgraph");
    if (!Array.isArray(rechtsgraph?.toolNames)) {
      throw new Error("RechtsGraph tool names unavailable for local_text_only validation");
    }
    if (rechtsgraph.toolNames.includes("explore_references")) {
      throw new Error("local_text_only must disable explore_references");
    }
  }
}
