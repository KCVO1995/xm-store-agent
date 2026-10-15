import { describe, expect, it } from "vitest";
import {
  buildAgentRuntimeRequest,
  omitAgentRuntimeConfig,
  readAgentRuntimeFormValues,
} from "./agentRuntimeConfig";

describe("agentRuntimeConfig", () => {
  it("readAgentRuntimeFormValues parses numeric config keys", () => {
    expect(
      readAgentRuntimeFormValues({
        max_iters: 10,
        max_input_length: "64000",
        temperature: 0.5,
        tool_result_token_limit: "50000",
      }),
    ).toEqual({
      max_iters: 10,
      max_input_length: 64000,
      temperature: 0.5,
      top_p: undefined,
      max_tokens: undefined,
      tool_result_token_limit: 50000,
    });
  });

  it("buildAgentRuntimeRequest exposes runtime values as top-level API fields", () => {
    expect(
      buildAgentRuntimeRequest({
        max_iters: 12,
        max_input_length: 32000,
        temperature: undefined,
        tool_result_token_limit: 40000,
      }),
    ).toEqual({
      max_iters: 12,
      max_input_length: 32000,
      tool_result_token_limit: 40000,
    });
  });

  it("omitAgentRuntimeConfig keeps runtime fields out of opaque config", () => {
    expect(
      omitAgentRuntimeConfig({
        max_iters: 12,
        temperature: 0.3,
        tool_result_token_limit: 50000,
        plugins: {},
      }),
    ).toEqual({ plugins: {} });
  });

  it("clears the tool result limit to restore the runtime default", () => {
    expect(buildAgentRuntimeRequest({}, { clearMissing: true })).toMatchObject({
      tool_result_token_limit: null,
    });
  });
});
