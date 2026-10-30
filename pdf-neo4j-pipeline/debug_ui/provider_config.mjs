const OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1";

export function modelProviderArgs(env = process.env) {
  const provider = String(env.RECHTSGRAPH_MODEL_PROVIDER || "").trim().toLowerCase();
  if (!provider) return [];
  if (provider !== "openrouter") {
    throw new Error(`Unsupported RECHTSGRAPH_MODEL_PROVIDER: ${provider}`);
  }
  return [
    "-c", 'model_provider="openrouter"',
    "-c", 'model_providers.openrouter.name="OpenRouter"',
    "-c", `model_providers.openrouter.base_url="${OPENROUTER_BASE_URL}"`,
    "-c", 'model_providers.openrouter.wire_api="responses"',
    "-c", 'model_providers.openrouter.env_key="OPENROUTER_API_KEY"',
    // Keep Codex on the Responses API's hosted `{type:"web_search"}` tool.
    // Its standalone-web mode instead calls `<base_url>/alpha/search`, which
    // OpenRouter does not expose.
  ];
}

export function configuredModelEntries(env = process.env) {
  return String(env.RECHTSGRAPH_CUSTOM_MODELS || env.RECHTSGRAPH_CUSTOM_MODEL || "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean)
    .map((id) => ({
      id,
      model: id,
      displayName: id,
      description: "Explicitly configured custom-provider model",
      defaultReasoningEffort: "low",
      supportedReasoningEfforts: ["low", "medium", "high"].map((reasoningEffort) => ({ reasoningEffort })),
      isDefault: false,
      configuredExternally: true,
    }));
}

export function providerMetadata(env = process.env) {
  const provider = String(env.RECHTSGRAPH_MODEL_PROVIDER || "").trim().toLowerCase();
  if (!provider) return { id: "codex-default", baseUrl: null, customModels: [] };
  return {
    id: provider,
    baseUrl: provider === "openrouter" ? OPENROUTER_BASE_URL : null,
    customModels: configuredModelEntries(env).map((entry) => entry.id),
  };
}
