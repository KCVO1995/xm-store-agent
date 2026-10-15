import { readNumber } from "./readNumber";

export const AGENT_RUNTIME_CONFIG_KEYS = [
  "max_iters",
  "max_input_length",
  "temperature",
  "top_p",
  "max_tokens",
  "tool_result_token_limit",
] as const;

export type AgentRuntimeConfigKey = (typeof AGENT_RUNTIME_CONFIG_KEYS)[number];

export type AgentRuntimeFormValues = Partial<
  Record<AgentRuntimeConfigKey, number>
>;
export type AgentRuntimeRequest = Partial<
  Record<AgentRuntimeConfigKey, number | null>
>;

export function readAgentRuntimeFormValues(
  cfg: object,
): AgentRuntimeFormValues {
  const record = cfg as Record<string, unknown>;
  return {
    max_iters: readNumber(record.max_iters),
    max_input_length: readNumber(record.max_input_length),
    temperature: readNumber(record.temperature),
    top_p: readNumber(record.top_p),
    max_tokens: readNumber(record.max_tokens),
    tool_result_token_limit: readNumber(record.tool_result_token_limit),
  };
}

export function buildAgentRuntimeRequest(
  values: AgentRuntimeFormValues,
  options?: { clearMissing?: boolean },
): AgentRuntimeRequest {
  const request: AgentRuntimeRequest = {};
  for (const key of AGENT_RUNTIME_CONFIG_KEYS) {
    const val = values[key];
    if (typeof val === "number" && Number.isFinite(val)) {
      request[key] = val;
    } else if (options?.clearMissing) {
      request[key] = null;
    }
  }
  return request;
}

export function omitAgentRuntimeConfig(
  config: Record<string, unknown>,
): Record<string, unknown> {
  const cleaned = { ...config };
  for (const key of AGENT_RUNTIME_CONFIG_KEYS) {
    delete cleaned[key];
  }
  return cleaned;
}
