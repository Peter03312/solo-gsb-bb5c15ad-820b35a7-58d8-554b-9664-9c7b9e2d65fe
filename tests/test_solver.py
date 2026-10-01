"""求解器与 PEP 440 版本模块测试。

无需数据库或第三方依赖：
    python3 tests/test_solver.py
若安装了 pytest，也可直接 ``pytest`` 运行。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.solver import (
    Candidate, CycleError, RuntimeRequirement, Snapshot, installation_order,
    rehearse, solve, UnsatisfiableError,
)
from app.version import (
    InvalidSpecifier, InvalidVersion, SpecifierSet, Version,
)


def R(name, spec):
    return RuntimeRequirement(name=name, spec=SpecifierSet.parse(spec))


def C(plugin, ver, deps=(), conflicts=(), rts=()):
    return Candidate(
        plugin=plugin,
        version=Version.parse(ver),
        runtimes=tuple(rts),
        dependencies=tuple((d, SpecifierSet.parse(s)) for d, s in deps),
        conflicts=tuple(conflicts),
    )


class VersionTests(unittest.TestCase):
    def test_ordering_pep440(self):
        seq = ["1.0.dev1", "1.0a1.dev1", "1.0a1", "1.0b1", "1.0rc1",
               "1.0", "1.0.post1.dev1", "1.0.post1"]
        parsed = [Version.parse(s) for s in seq]
        self.assertEqual(parsed, sorted(parsed))

    def test_equality_and_epoch(self):
        self.assertEqual(Version.parse("1.4"), Version.parse("1.4.0"))
        self.assertGreater(Version.parse("1!0"), Version.parse("99"))
        self.assertEqual(str(Version.parse("1.0-rc")), "1.0rc0")

    def test_specifiers(self):
        s = SpecifierSet.parse(">=1.0,<2.0")
        self.assertTrue(s.contains(Version.parse("1.5")))
        self.assertFalse(s.contains(Version.parse("2.0")))
        compat = SpecifierSet.parse("~=1.4.2")
        self.assertTrue(compat.contains(Version.parse("1.4.9")))
        self.assertFalse(compat.contains(Version.parse("1.5")))
        self.assertTrue(SpecifierSet.parse("==1.0.*").contains(Version.parse("1.0.9")))
        self.assertTrue(SpecifierSet.parse("3.8.*").contains(Version.parse("3.8.12")))
        self.assertTrue(SpecifierSet.parse("").contains(Version.parse("9.9")))

    def test_invalid(self):
        for bad in ["abc", "1..0"]:
            with self.assertRaises(InvalidVersion):
                Version.parse(bad)
        for bad in [">>", "~=1", "==1.0.*,<x", "<>1.0"]:
            with self.assertRaises(InvalidSpecifier):
                SpecifierSet.parse(bad)


class SolverTests(unittest.TestCase):
    def setUp(self):
        self.basic = Snapshot(1, {
            "alpha": (C("alpha", "1.0", [("beta", ">=1")]),),
            "beta": (C("beta", "1.5", [("gamma", ">=2")]),
                     C("beta", "1.0", [("gamma", ">=1")])),
            "gamma": (C("gamma", "2.0"), C("gamma", "1.0")),
        })

    def plan(self, snap, runtimes=(), **targets):
        return solve(snap, list(runtimes),
                     {k: SpecifierSet.parse(v) for k, v in targets.items()})

    def test_transitive_latest_and_install_order(self):
        plan = self.plan(self.basic, alpha="*")
        self.assertEqual({p: str(c.version) for p, c in plan.items()},
                         {"alpha": "1.0", "beta": "1.5", "gamma": "2.0"})
        self.assertEqual(installation_order(plan), ["gamma", "beta", "alpha"])

    def test_intersection_forces_downgrade(self):
        plan = self.plan(self.basic, alpha="*", gamma="<2")
        self.assertEqual(str(plan["beta"].version), "1.0")
        self.assertEqual(str(plan["gamma"].version), "1.0")

    def test_impossible_target(self):
        with self.assertRaises(UnsatisfiableError) as cm:
            self.plan(self.basic, gamma=">=9")
        reasons = cm.exception.reasons
        self.assertTrue(any(r["type"] == "no_matching_version"
                            and r["plugin"] == "gamma" for r in reasons))

    def test_missing_plugin(self):
        with self.assertRaises(UnsatisfiableError) as cm:
            self.plan(self.basic, ghost="")
        self.assertTrue(any(r["type"] == "missing_plugin"
                            and r["plugin"] == "ghost"
                            for r in cm.exception.reasons))

    def test_mutex_blocks(self):
        snap = Snapshot(1, {
            "a": (C("a", "1.0", [("b", ">=1"), ("d", ">=1")]),),
            "b": (C("b", "1.0", conflicts=["d"]),),
            "d": (C("d", "1.0"),),
        })
        with self.assertRaises(UnsatisfiableError) as cm:
            self.plan(snap, a="")
        self.assertTrue(any(r["type"] == "mutex_conflict"
                            for r in cm.exception.reasons))

    def test_mutex_avoids_via_other_version(self):
        snap = Snapshot(1, {
            "a": (C("a", "1.0", [("b", ">=1"), ("d", ">=1")]),),
            "b": (C("b", "2.0"), C("b", "1.0", conflicts=["d"])),
            "d": (C("d", "1.0"),),
        })
        plan = self.plan(snap, a="")
        self.assertEqual(str(plan["b"].version), "2.0")

    def test_dependency_cycle_explicit_error(self):
        snap = Snapshot(1, {
            "a": (C("a", "1.0", [("b", ">=1")]),),
            "b": (C("b", "1.0", [("a", ">=1")]),),
        })
        with self.assertRaises(CycleError) as cm:
            self.plan(snap, a="")
        cyc = cm.exception.reasons[0]["cycle"]
        self.assertEqual(cyc[0], cyc[-1])

    def test_cycle_on_newer_picks_older_acyclic(self):
        snap = Snapshot(1, {
            "a": (C("a", "2.0", [("b", ">=2")]),
                  C("a", "1.0", [("b", ">=1")])),
            "b": (C("b", "2.0", [("a", ">=2")]), C("b", "1.0")),
        })
        plan = self.plan(snap, a="")
        self.assertEqual({p: str(c.version) for p, c in plan.items()},
                         {"a": "1.0", "b": "1.0"})

    def test_runtime_compatibility_selects_older(self):
        snap = Snapshot(1, {
            "a": (C("a", "2.0", rts=[R("python", ">=3.9")]),
                  C("a", "1.0", rts=[R("python", ">=3.6,<3.9")])),
        })
        plan = self.plan(snap, runtimes=[R("python", "3.8.*")], a="")
        self.assertEqual(str(plan["a"].version), "1.0")

    def test_runtime_no_match_is_unsat_with_reason(self):
        snap = Snapshot(1, {
            "a": (C("a", "1.0", rts=[R("python", ">=3.9")]),),
        })
        with self.assertRaises(UnsatisfiableError) as cm:
            self.plan(snap, runtimes=[R("python", "2.7")], a="")
        self.assertTrue(any("python" in r.get("runtime_blocked", [])
                            for r in cm.exception.reasons))

    def test_diamond_disjoint_ranges(self):
        snap = Snapshot(1, {
            "a": (C("a", "1.0", [("b", ""), ("d", "")]),),
            "b": (C("b", "1.0", [("gamma", ">=2,<3")]),),
            "d": (C("d", "1.0", [("gamma", ">=3,<4")]),),
            "gamma": (C("gamma", "3.5"), C("gamma", "2.5")),
        })
        with self.assertRaises(UnsatisfiableError):
            self.plan(snap, a="")

    def test_deep_backtracking(self):
        # 钉住 gamma<2：a2/b2 链不可行，必须回溯到 a1/b1/gamma1
        snap = Snapshot(1, {
            "a": (C("a", "2.0", [("beta", ">=2")]),
                  C("a", "1.0", [("beta", ">=1")])),
            "beta": (C("beta", "2.0", [("gamma", ">=2")]),
                     C("beta", "1.0", [("gamma", ">=1")])),
            "gamma": (C("gamma", "2.0"), C("gamma", "1.0")),
        })
        plan = self.plan(snap, a="", gamma="<2")
        self.assertEqual({p: str(c.version) for p, c in plan.items()},
                         {"a": "1.0", "beta": "1.0", "gamma": "1.0"})

    def test_deterministic(self):
        first = {p: str(c.version) for p, c in self.plan(self.basic, alpha="*").items()}
        second = {p: str(c.version) for p, c in self.plan(self.basic, alpha="*").items()}
        self.assertEqual(first, second)

    def test_self_dependency_unsat(self):
        snap = Snapshot(1, {"a": (C("a", "1.0", [("a", ">=2")]),)})
        with self.assertRaises(UnsatisfiableError):
            self.plan(snap, a="")

    def test_multiple_runtimes_must_all_match(self):
        snap = Snapshot(1, {
            "a": (C("a", "1.0", rts=[R("python", ">=3.9"), R("cuda", ">=11")]),),
        })
        ok = self.plan(snap, runtimes=[R("python", "3.10"), R("cuda", "12")], a="")
        self.assertEqual(str(ok["a"].version), "1.0")
        with self.assertRaises(UnsatisfiableError):
            self.plan(snap, runtimes=[R("python", "3.10"), R("cuda", "10")], a="")


class RehearseTests(unittest.TestCase):
    """最小变动预演：保留 > 新增/移除/换版，平手沿用默认确定性选择。"""

    def setUp(self):
        # lib 1.0/2.0 均满足 app 的 >=1；默认求解选最新 lib 2.0
        self.snap = Snapshot(7, {
            "lib": (C("lib", "2.0"), C("lib", "1.0")),
            "app": (C("app", "1.0", [("lib", ">=1")]),),
            "extra": (C("extra", "1.0"),),
        })
        self.targets = {"app": SpecifierSet.parse("")}

    def versions(self, plan):
        return {p: str(c.version) for p, c in plan.items()}

    def test_identical_request_keeps_everything(self):
        base = {"app": "1.0", "lib": "2.0"}
        plan = rehearse(self.snap, [], self.targets, base)
        self.assertEqual(self.versions(plan), {"app": "1.0", "lib": "2.0"})

    def test_keep_older_baseline_over_newest_default(self):
        # 默认规则会升级到 lib 2.0，但保留 lib 1.0 可行（代价 0）
        base = {"app": "1.0", "lib": "1.0"}
        plan = rehearse(self.snap, [], self.targets, base)
        self.assertEqual(self.versions(plan), {"app": "1.0", "lib": "1.0"})

    def test_change_only_when_required(self):
        # lib 1.0 在目标修订不可见（撤销模拟）：仅 lib 换版，app 保留
        snap = Snapshot(8, {
            "lib": (C("lib", "2.0"),),
            "app": (C("app", "1.0", [("lib", ">=1")]),),
        })
        plan = rehearse(snap, [], self.targets, {"app": "1.0", "lib": "1.0"})
        self.assertEqual(self.versions(plan), {"app": "1.0", "lib": "2.0"})

    def test_removal_of_unneeded_plugin(self):
        # 原方案含 extra，新目标不再需要且无人依赖 -> extra 被移除
        base = {"app": "1.0", "lib": "2.0", "extra": "1.0"}
        plan = rehearse(self.snap, [], self.targets, base)
        self.assertEqual(self.versions(plan), {"app": "1.0", "lib": "2.0"})

    def test_addition_for_new_target(self):
        targets = {"app": SpecifierSet.parse(""),
                   "extra": SpecifierSet.parse("")}
        plan = rehearse(self.snap, [], targets, {"app": "1.0", "lib": "2.0"})
        self.assertEqual(self.versions(plan),
                         {"app": "1.0", "lib": "2.0", "extra": "1.0"})

    def test_runtime_override_forces_minimal_downgrade_chain(self):
        snap = Snapshot(9, {
            "alpha": (C("alpha", "2.0", rts=[R("python", ">=3.10")]),
                      C("alpha", "1.0", rts=[R("python", ">=3.8")])),
            "beta": (C("beta", "2.0", [("alpha", ">=2")],
                       rts=[R("python", ">=3.10")]),
                     C("beta", "1.0", [("alpha", ">=1")],
                       rts=[R("python", ">=3.8")])),
        })
        base = {"alpha": "2.0", "beta": "2.0"}
        plan = rehearse(snap, [R("python", "3.9.*")],
                        {"beta": SpecifierSet.parse("")}, base)
        self.assertEqual(self.versions(plan), {"alpha": "1.0", "beta": "1.0"})

    def test_no_baseline_matches_default_solution(self):
        default_ = self.versions(solve(self.snap, [], self.targets))
        plan = self.versions(rehearse(self.snap, [], self.targets, {}))
        self.assertEqual(plan, default_)
        self.assertEqual(plan["lib"], "2.0")  # 平手走版本降序

    def test_deterministic(self):
        base = {"app": "1.0", "lib": "1.0"}
        first = self.versions(rehearse(self.snap, [], self.targets, base))
        second = self.versions(rehearse(self.snap, [], self.targets, base))
        self.assertEqual(first, second)

    def test_unsat_raises_structured_error(self):
        snap = Snapshot(8, {"lib": (C("lib", "2.0"),)})
        with self.assertRaises(UnsatisfiableError) as cm:
            rehearse(snap, [], {"lib": SpecifierSet.parse(">=9")},
                     {"lib": "2.0"})
        self.assertTrue(any(r["type"] == "no_matching_version"
                            for r in cm.exception.reasons))

    def test_cycle_raises_cycle_error(self):
        snap = Snapshot(1, {
            "a": (C("a", "1.0", [("b", ">=1")]),),
            "b": (C("b", "1.0", [("a", ">=1")]),),
        })
        with self.assertRaises(CycleError):
            rehearse(snap, [], {"a": SpecifierSet.parse("")}, {"a": "1.0"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
