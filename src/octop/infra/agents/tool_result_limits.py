"""Configure filesystem result limits through deepagents' middleware override."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from deepagents.middleware.filesystem import FilesystemMiddleware
from harness_agent import HarnessAgentConfig
from harness_agent.backends import resolve_backend, spec_supports_execution

from octop.infra.agents.runtime_limits import agent_tool_result_token_limit


def apply_tool_result_limit(
    config: HarnessAgentConfig, agent_config: dict[str, Any], *, workspace_dir: Path
) -> HarnessAgentConfig:
    """Replace the default filesystem middleware only for an explicit override.

    Share the resolved backend with harness so artifact routing and remote
    sandbox instances remain the same. Execution backends retain harness's
    FilesystemGuardMiddleware; other backends enforce permissions here.
    """
    limit = agent_tool_result_token_limit(agent_config)
    if limit is None:
        return config
    backend = resolve_backend(
        config.backend,
        workspace_dir=workspace_dir,
        system_files_path=config.system_files_path,
    )
    filesystem = FilesystemMiddleware(
        backend=backend,
        tool_token_limit_before_evict=limit,
        _permissions=None if spec_supports_execution(backend) else config.permissions,
    )
    # deepagents replaces the existing slot by middleware name, including the
    # built-in general-purpose subagent's slot, rather than adding duplicate tools.
    return replace(config, backend=backend, middleware=[*(config.middleware or []), filesystem])
