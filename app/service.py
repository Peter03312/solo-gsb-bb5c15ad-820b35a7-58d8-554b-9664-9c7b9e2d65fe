"""求解服务：从快照构造求解器输入、执行求解、持久化方案。"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import catalog, repository
from .models import Plugin, PluginVersion
from .solver import (
    RuntimeRequirement, installation_order, solve as solver_run,
)
from .version import InvalidSpecifier, SpecifierSet


class RequestError(ValueError):
    """请求参数问题（400）。"""


@dataclass
class SolveOutcome:
    solution_row: object
    cached: bool
    selection: dict
    install_order: list[str]
    revision: int


def _parse_targets(targets: dict[str, str]) -> dict[str, SpecifierSet]:
    parsed: dict[str, SpecifierSet] = {}
    for name, spec in targets.items():
        try:
            parsed[name] = SpecifierSet.parse(spec or "")
        except InvalidSpecifier as e:
            raise RequestError(f"目标插件 {name} 的版本范围无效: {e}") from e
    return parsed


def _parse_runtimes(runtimes) -> list[RuntimeRequirement]:
    out: list[RuntimeRequirement] = []
    seen = set()
    for rt in runtimes:
        if rt.name in seen:
            raise RequestError(f"运行环境 {rt.name} 重复提交")
        seen.add(rt.name)
        try:
            out.append(RuntimeRequirement(name=rt.name,
                                          spec=SpecifierSet.parse(rt.spec or "")))
        except InvalidSpecifier as e:
            raise RequestError(f"运行环境 {rt.name} 的版本范围无效: {e}") from e
    return out


def resolve_revision(db: Session, requested_revision: int | None) -> int:
    if requested_revision is not None:
        from .models import CatalogRevision
        exists = db.execute(
            select(CatalogRevision.revision)
            .where(CatalogRevision.revision == requested_revision)
        ).scalar_one_or_none()
        if exists is None:
            raise RequestError(f"目录修订 {requested_revision} 不存在")
        return requested_revision
    return repository.latest_revision(db)


def execute(db: Session, *, revision: int,
            runtimes_in, targets_in: dict[str, str]) -> SolveOutcome:
    runtime_reqs = _parse_runtimes(runtimes_in)
    target_specs = _parse_targets(targets_in)

    request_hash, canonical = catalog.canonical_request(runtime_reqs, targets_in)
    canonical_with_revision = {"revision": revision, **canonical}

    cached = repository.get_solution(db, revision, request_hash)
    if cached is not None:
        return SolveOutcome(
            solution_row=cached, cached=True,
            selection=cached.selection, install_order=cached.install_order,
            revision=revision,
        )

    snapshot = repository.load_snapshot(db, revision)
    plan = solver_run(snapshot, runtime_reqs, target_specs)
    order = installation_order(plan)

    # 查版本元数据（sha256 等）并构造可序列化结果
    keys = [(name, str(cand.version)) for name, cand in plan.items()]
    rows = db.execute(
        select(PluginVersion, Plugin.name)
        .join(Plugin, Plugin.id == PluginVersion.plugin_id)
        .where(
            Plugin.name.in_([k[0] for k in keys]),
            PluginVersion.version.in_([k[1] for k in keys]),
        )
    ).all()
    lookup = {(name, pv.version): pv for pv, name in rows}

    selection: dict = {}
    version_ids = []
    for name in sorted(plan):
        cand = plan[name]
        pv = lookup[(name, str(cand.version))]
        version_ids.append(pv.id)
        selection[name] = {
            "plugin": name,
            "version": str(cand.version),
            "sha256": pv.sha256,
            "dependencies": [
                {"plugin": d, "spec": str(spec)} for d, spec in cand.dependencies
            ],
            "runtimes": [
                {"name": r.name, "spec": str(r.spec)} for r in cand.runtimes
            ],
            "conflicts": list(cand.conflicts),
            "package_url": f"/api/v1/plugins/{name}/versions/{str(cand.version)}/download",
            "version_id": pv.id,
        }

    sol = repository.save_solution(
        db,
        revision=revision,
        request_hash=request_hash,
        request=canonical_with_revision,
        selection=selection,
        install_order=order,
    )
    return SolveOutcome(solution_row=sol, cached=False, selection=selection,
                        install_order=order, revision=revision)


def annotate_revoked(db: Session, selection: dict) -> dict:
    """给历史方案中的每个选中项补上“当前是否已撤销”的标记。"""
    flags = repository.revoked_flags(
        db, [v["version_id"] for v in selection.values()])
    out = {}
    for name, item in selection.items():
        item = dict(item)
        item["revoked"] = flags.get(item["version_id"], False)
        item.pop("version_id", None)
        out[name] = item
    return out
