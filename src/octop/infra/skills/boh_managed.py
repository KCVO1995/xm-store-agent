"""Versioned BOH Skill package, mounted only on company store-assistant agents."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from octop.infra.agents.profile import dump_skill_package_ids, id_list_from_row
from octop.infra.skills.skill_package_store import SkillPackageStore
from octop.infra.utils.paths import PathLayout

PACKAGE_NAME = "BOH 门店分差分析（系统管理）"
SKILL_SLUG = "boh-store-variance"


def ensure_boh_skill_package(services: Any, paths: PathLayout) -> str:
    store = SkillPackageStore(repo=services.skill_package_repo, root=paths.skill_packages_dir)
    row = services.skill_package_repo.get_by_name(PACKAGE_NAME)
    if row is None:
        row = store.create(
            name=PACKAGE_NAME,
            description="当前会话门店的 BOH 原始报表取数与确定性分差分析",
            created_by="system",
        )
    source = Path(__file__).parent / "assets" / SKILL_SLUG / "SKILL.md"
    target = store.package_skills_dir(row.id) / SKILL_SLUG / "SKILL.md"
    content = source.read_bytes()
    if not target.exists() or target.read_bytes() != content:
        store.write_skill(row.id, SKILL_SLUG, [("SKILL.md", content)])
    return str(row.id)


def mount_existing_store_assistants(services: Any, package_id: str) -> None:
    for row in services.agent_repo.list_all():
        if row.user_id is None or row.name != "门店助手":
            continue
        company = services.connector_repo.get_by_user_kind(row.user_id, "xm-store")
        if company is None or company.status != "active" or not company.has_credentials:
            continue
        ids = id_list_from_row(row, "skill_package_ids")
        if package_id not in ids:
            services.agent_repo.update_config(
                row.agent_id, skill_package_ids=dump_skill_package_ids([*ids, package_id])
            )
