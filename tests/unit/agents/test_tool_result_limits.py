"""Exercise configured result limits through the real deepagents graph/tools."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemPermission
from harness_agent import HarnessAgentConfig
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from octop.infra.agents.tool_result_limits import apply_tool_result_limit


class ToolChatModel(FakeMessagesListChatModel):
    def bind_tools(self, *_args, **_kwargs):
        return self


@pytest.mark.parametrize("token_limit,offloaded", [(2_000, True), (4_000, False)])
@pytest.mark.asyncio
async def test_configured_limit_controls_real_graph_tool_results(tmp_path, token_limit, offloaded):
    content = "row: data\n" * 1_200

    @tool
    def course_report() -> str:
        """Return a course report."""
        return content

    model = ToolChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "course_report", "args": {}, "id": "report-call"}],
            ),
            AIMessage(content="done"),
        ]
    )
    config = apply_tool_result_limit(
        HarnessAgentConfig(
            workspace_dir=tmp_path,
            backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        ),
        {"tool_result_token_limit": token_limit},
        workspace_dir=tmp_path,
    )
    graph = create_deep_agent(
        model=model, tools=[course_report], backend=config.backend, middleware=config.middleware
    )
    result = await graph.ainvoke({"messages": [HumanMessage(content="query courses")]})
    message = next(m for m in result["messages"] if isinstance(m, ToolMessage))
    if offloaded:
        assert "large_tool_results/report-call" in message.content
        assert (tmp_path / "large_tool_results" / "report-call").read_text() == content
    else:
        assert message.content == content
        assert not (tmp_path / "large_tool_results").exists()


def test_read_file_limit_and_permissions_are_preserved(tmp_path: Path):
    backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    # Line-based paging can advance at a lower cap and read all lines at a higher cap.
    content = "entry:" + "x" * 250 + "\n"
    assert backend.write("/report.txt", content * 50).error is None
    assert backend.write("/secret.txt", "secret").error is None
    original = HarnessAgentConfig(
        workspace_dir=tmp_path,
        backend=backend,
        permissions=[FilesystemPermission(operations=["read"], paths=["/secret.txt"], mode="deny")],
    )
    outputs = []
    for limit in (1_000, 5_000):
        config = apply_tool_result_limit(
            original, {"tool_result_token_limit": limit}, workspace_dir=tmp_path
        )
        assert config.backend is backend
        filesystem = next(m for m in config.middleware if isinstance(m, FilesystemMiddleware))
        read_file = next(t for t in filesystem.tools if t.name == "read_file")
        runtime = SimpleNamespace(tool_call_id="read-call")
        result = read_file.func(file_path="/report.txt", runtime=runtime, limit=100)
        outputs.append(result.content)
        denied = read_file.func(file_path="/secret.txt", runtime=runtime)
        assert denied.status == "error"
        assert "permission denied" in denied.content
    assert outputs[0].count("entry:") < 50
    assert outputs[1].count("entry:") == 50


def test_no_override_keeps_harness_defaults(tmp_path: Path):
    original = HarnessAgentConfig(workspace_dir=tmp_path)
    assert apply_tool_result_limit(original, {}, workspace_dir=tmp_path) is original
    assert (
        apply_tool_result_limit(original, {"tool_result_token_limit": None}, workspace_dir=tmp_path)
        is original
    )


def test_offload_artifacts_stay_in_host_workspace(tmp_path: Path):
    root_dir = tmp_path / "storage"
    host_workspace = root_dir / "workspaces" / "expert"
    host_workspace.mkdir(parents=True)
    config = apply_tool_result_limit(
        HarnessAgentConfig(
            workspace_dir=tmp_path / "agent-visible-workspace",
            system_files_path=".octop",
            backend={"type": "filesystem", "root_dir": str(root_dir), "virtual_mode": True},
        ),
        {"tool_result_token_limit": 1_000},
        workspace_dir=host_workspace,
    )
    filesystem = next(m for m in config.middleware if isinstance(m, FilesystemMiddleware))
    from langgraph.prebuilt.tool_node import ToolCallRequest

    request = ToolCallRequest(
        tool_call={"name": "course_report", "args": {}, "id": "report-call"},
        tool=None,
        state={},
        runtime=None,
    )
    content = "course data\n" * 1_000
    filesystem.wrap_tool_call(
        request, lambda _: ToolMessage(content=content, tool_call_id="report-call")
    )
    assert (host_workspace / ".octop" / "large_tool_results" / "report-call").read_text() == content
    assert not (root_dir / "large_tool_results").exists()
