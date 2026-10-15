import { App, Form, Input } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import {
  afterAll,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import { request } from "../../../api/request";
import type { OctopAgent } from "../../../context/AgentContext";
import EditAgentDrawer from "./EditAgentDrawer";

vi.mock("react-i18next", () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t, i18n: { language: "zh" } }) };
});
vi.mock("../../../api/request", () => ({ request: vi.fn() }));
vi.mock("../../../context/AgentContext", () => ({
  useAgent: () => ({ refresh: vi.fn() }),
}));
vi.mock("../../../hooks/useAgentFormResources", () => ({
  useAgentFormResources: () => ({
    models: [],
    modelsLoading: false,
    backends: [],
    backendsLoading: false,
  }),
}));
vi.mock("../../../api/modules/skillPackages", () => ({
  skillPackagesApi: { replaceMounted: vi.fn(async () => undefined) },
}));
vi.mock("../../Agent/Skills/skillDisplayNames", () => ({
  useSkillDisplayName: () => (skill: { name: string }) => skill.name,
}));
vi.mock("./AgentBackendFields", () => ({
  default: () => (
    <Form.Item name="backend_choice" hidden>
      <Input />
    </Form.Item>
  ),
}));
vi.mock("../../../components/ExpertColorPicker", () => ({
  default: () => null,
}));
vi.mock("./ExpertAvatarPicker", () => ({ default: () => null }));
vi.mock("./ExpertComposerDefaultsFields", () => ({ default: () => null }));
vi.mock("../../Agent/Workspace/components/WorkspaceDrawer", () => ({
  default: () => null,
}));
vi.mock("./SkillCatalogDrawer", () => ({ default: () => null }));
vi.mock("./SkillSourcePickerModal", () => ({ default: () => null }));
vi.mock("./SubagentCatalogDrawer", () => ({ default: () => null }));
vi.mock("./FileEditModal", () => ({ default: () => null }));

const agent = {
  agent_id: "agent-1",
  name: "Test expert",
  state: "stopped",
  color: null,
  icon_url: null,
} as OctopAgent;

describe("EditAgentDrawer runtime persistence", () => {
  const getComputedStyle = window.getComputedStyle;

  beforeAll(() => {
    vi.spyOn(window, "getComputedStyle").mockImplementation((element) =>
      getComputedStyle(element),
    );
  });
  afterAll(() => {
    vi.restoreAllMocks();
  });

  let detail: Record<string, unknown>;
  const onClose = vi.fn();
  const onSaved = vi.fn();
  const drawer = (open = true) => (
    <App>
      <EditAgentDrawer
        open={open}
        agent={agent}
        onClose={onClose}
        onSaved={onSaved}
      />
    </App>
  );

  beforeEach(() => {
    vi.clearAllMocks();
    detail = {
      ...agent,
      name: "Test expert",
      description: "",
      default_model: null,
      tool_result_token_limit: 50000,
      max_iters: 20,
      temperature: 0.3,
      config: { backend: { type: "named", name: "test-storage" } },
    };
    vi.mocked(request).mockImplementation(async (_path, options) => {
      if (options?.method === "PATCH") {
        detail = { ...detail, ...JSON.parse(options.body as string) };
      }
      return { ...detail } as never;
    });
  });

  it("keeps loaded runtime values when saving without expanding advanced options", async () => {
    const view = render(drawer());
    const name = await screen.findByDisplayValue("Test expert");
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
    fireEvent.change(name, { target: { value: "Renamed expert" } });
    fireEvent.click(screen.getByRole("button", { name: "common.save" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(detail).toMatchObject({
      tool_result_token_limit: 50000,
      max_iters: 20,
      temperature: 0.3,
    });

    view.rerender(drawer(false));
    view.rerender(drawer());
    await screen.findByDisplayValue("Renamed expert");
    fireEvent.click(screen.getByText("experts.advancedOptions"));
    const limit = await screen.findByRole("spinbutton", {
      name: /^agentConfig.toolResultTokenLimit/,
    });
    expect(limit).toHaveValue("50000");
  });

  it.each([
    ["60000", 60000],
    ["", null],
  ])(
    "persists an edited limit %s after closing and reopening",
    async (input, expected) => {
      const view = render(drawer());
      await screen.findByDisplayValue("Test expert");
      fireEvent.click(screen.getByText("experts.advancedOptions"));
      const limit = await screen.findByRole("spinbutton", {
        name: /^agentConfig.toolResultTokenLimit/,
      });
      fireEvent.change(limit, { target: { value: input } });
      fireEvent.click(screen.getByRole("button", { name: "common.save" }));
      await waitFor(() => expect(onSaved).toHaveBeenCalled());
      expect(detail.tool_result_token_limit).toBe(expected);

      view.rerender(drawer(false));
      view.rerender(drawer());
      await screen.findByDisplayValue("Test expert");
      fireEvent.click(screen.getByText("experts.advancedOptions"));
      expect(
        await screen.findByRole("spinbutton", {
          name: /^agentConfig.toolResultTokenLimit/,
        }),
      ).toHaveValue(expected === null ? "" : String(expected));
    },
  );
});
