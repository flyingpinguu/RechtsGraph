import assert from "node:assert/strict";
import test from "node:test";

import { configuredModelEntries, modelProviderArgs, providerMetadata } from "../provider_config.mjs";

test("default provider configuration adds no runtime overrides", () => {
  assert.deepEqual(modelProviderArgs({}), []);
  assert.deepEqual(configuredModelEntries({}), []);
  assert.deepEqual(providerMetadata({}), { id: "codex-default", baseUrl: null, customModels: [] });
});

test("OpenRouter configuration uses an environment key without exposing its value", () => {
  const env = {
    RECHTSGRAPH_MODEL_PROVIDER: "openrouter",
    RECHTSGRAPH_CUSTOM_MODEL: "z-ai/glm-5.3-flash",
    OPENROUTER_API_KEY: "must-not-appear",
  };
  const args = modelProviderArgs(env);
  assert(args.includes('model_provider="openrouter"'));
  assert(args.includes('model_providers.openrouter.env_key="OPENROUTER_API_KEY"'));
  assert(!args.some((value) => value.includes("supports_standalone_web_search")));
  assert(!args.some((value) => value.includes("features.standalone_web_search")));
  assert(!JSON.stringify(args).includes(env.OPENROUTER_API_KEY));
  assert.equal(configuredModelEntries(env)[0].id, "z-ai/glm-5.3-flash");
  assert.deepEqual(providerMetadata(env).customModels, ["z-ai/glm-5.3-flash"]);
});

test("unknown custom providers fail closed", () => {
  assert.throws(() => modelProviderArgs({ RECHTSGRAPH_MODEL_PROVIDER: "example" }), /Unsupported/);
});
