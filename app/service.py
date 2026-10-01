"""求解服务：从快照构造求解器输入、执行求解、持久化方案。"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import catalog, repository
from .models import CatalogRevision, Plugin, PluginVersion, Solution
from .solver import (
    CycleError, RuntimeRequirement, UnsatisfiableError, installation_order,
    rehearse as solver_rehearse, solve as solver_run,
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
    selection, version_ids = _build_selection(db, plan)

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


# ---------------------------------------------------------------- 预演（不保存）


@dataclass
class RehearsalOutcome:
    baseline_solution_id: int
    baseline_revision: int
    revision: int
    request_hash: str
    runtimes: list[RuntimeRequirement]
    targets: dict[str, str]
    runtimes_overridden: bool
    targets_overridden: bool
    selection: dict
    install_order: list[str]
    diff: list[dict]
    changes: dict


def _runtime_in_from_req(item) -> object:
    """规范化历史方案中保存的运行环境项，供 _parse_runtimes 复用。"""
    from .schemas import RuntimeIn
    if isinstance(item, RuntimeIn):
        return item
    return RuntimeIn(name=item["name"], spec=item.get("spec", ""))


def _effective_runtimes(original_request: dict, overrides) -> tuple[list[RuntimeRequirement], bool]:
    """覆盖运行环境列表：传入即整体替换（含空列表），缺省沿用原方案。"""
    if overrides is None:
        raw = original_request.get("runtimes", [])
        return _parse_runtimes([_runtime_in_from_req(r) for r in raw]), False
    return _parse_runtimes(overrides), True


def _effective_targets(original_request: dict,
                       overrides: dict[str, str] | None) -> tuple[dict[str, str], bool]:
    """覆盖目标插件约束：传入即整体替换（含空对象将被拒绝），缺省沿用原方案。"""
    if overrides is None:
        return dict(original_request.get("targets", {})), False
    if not overrides:
        raise RequestError("覆盖目标约束 targets 不能为空")
    return dict(overrides), True


def load_baseline_solution(db: Session, solution_id: int) -> Solution:
    row = db.get(Solution, solution_id)
    if row is None:
        raise RequestError(f"方案 {solution_id} 不存在")
    if not isinstance(row.request, dict) or "targets" not in row.request:
        raise RequestError(f"方案 {solution_id} 不是可预演的求解方案")
    return row


def resolve_revision_exists(db: Session, revision: int) -> None:
    exists = db.execute(
        select(CatalogRevision.revision)
        .where(CatalogRevision.revision == revision)
    ).scalar_one_or_none()
    if exists is None:
        raise RequestError(f"目录修订 {revision} 不存在")


def rehearse(db: Session, *, solution_id: int, target_revision: int,
             runtimes_in, targets_in: dict[str, str] | None) -> RehearsalOutcome:
    """基于已保存方案在目标修订上做预演；不落库、不写部分方案。"""
    baseline = load_baseline_solution(db, solution_id)
    resolve_revision_exists(db, target_revision)

    runtime_reqs, runtimes_overridden = _effective_runtimes(baseline.request, runtimes_in)
    target_strings, targets_overridden = _effective_targets(baseline.request, targets_in)
    if not target_strings:
        raise RequestError("目标插件约束不能为空")
    target_specs = _parse_targets(target_strings)

    request_hash, _canonical = catalog.canonical_request(runtime_reqs, target_strings)

    # 原方案作为“保留/换版/移除”的基线：plugin -> 原版本
    base_versions = {
        name: item["version"] for name, item in baseline.selection.items()
    }

    snapshot = repository.load_snapshot(db, target_revision)
    try:
        plan = solver_rehearse(snapshot, runtime_reqs, target_specs, base_versions)
    except (UnsatisfiableError, CycleError) as e:
        e.request_hash = request_hash  # 供 HTTP 错误回显
        raise
    order = installation_order(plan)
    selection, _version_ids = _build_selection(db, plan)
    for item in selection.values():
        item.pop("version_id", None)
        item["revoked"] = False  # 预演只选用目标修订内可见（未撤销）版本

    diff, changes = build_diff(base_versions, selection)

    return RehearsalOutcome(
        baseline_solution_id=solution_id,
        baseline_revision=baseline.revision_id,
        revision=target_revision,
        request_hash=request_hash,
        runtimes=runtime_reqs,
        targets=target_strings,
        runtimes_overridden=runtimes_overridden,
        targets_overridden=targets_overridden,
        selection=selection,
        install_order=order,
        diff=diff,
        changes=changes,
    )


def build_diff(base_versions: dict[str, str], selection: dict):
    """逐插件差异：kept 保留 / added 新增 / removed 移除 / changed 换版。"""
    diff: list[dict] = []
    counts = {"kept": 0, "added": 0, "removed": 0, "changed": 0}
    for name in sorted(set(base_versions) | set(selection)):
        old_v = base_versions.get(name)
        new_item = selection.get(name)
        new_v = new_item["version"] if new_item else None
        if old_v is None:
            kind = "added"
        elif new_item is None:
            kind = "removed"
        elif new_v != old_v:
            kind = "changed"
        else:
            kind = "kept"
        counts[kind] += 1
        diff.append({
            "plugin": name,
            "change": kind,
            "from_version": old_v,
            "to_version": new_v,
        })
    counts["total"] = counts["added"] + counts["removed"] + counts["changed"]
    return diff, counts


# ---------------------------------------------------------------- 序列化


def _build_selection(db: Session, plan: dict) -> tuple[dict, list]:
    """查版本元数据（sha256 等）并构造可序列化结果；返回 (selection, version_ids)。"""
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
    return selection, version_ids


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
