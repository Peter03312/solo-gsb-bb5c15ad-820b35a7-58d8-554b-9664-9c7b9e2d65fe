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
    solve, UnsatisfiableError,
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
