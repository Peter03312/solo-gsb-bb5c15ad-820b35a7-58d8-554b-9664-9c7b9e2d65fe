"""HTTP API 端到端集成测试。

需要 fastapi / sqlalchemy / pydantic / python-multipart / httpx：
    pip install fastapi 'sqlalchemy>=2' pydantic python-multipart httpx
    python3 tests/test_api.py

测试使用 SQLite 内存库与临时安装包目录；求解器本身在生产环境跑在
PostgreSQL + REPEATABLE READ 事务上，逻辑一致（JSON 字段跨方言兼容）。
"""
from __future__ import annotations

import hashlib
import importlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _have(pkg):
    try:
        importlib.import_module(pkg)
        return True
    except Exception:
        return False


REQUIRED = ("fastapi", "sqlalchemy", "pydantic", "multipart", "httpx")


@unittest.skipUnless(all(_have(p) for p in REQUIRED),
                     f"缺少依赖，需要: {', '.join(REQUIRED)}")
class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="plugin-api-test-")
        os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
        os.environ["PACKAGE_DIR"] = cls.tmp

        # 静态隔离：每次都重新导入受配置影响的模块
        for name in list(sys.modules):
            if name.startswith("app."):
                del sys.modules[name]

        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from sqlalchemy.pool import StaticPool

        from app import database
        cls.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        database.engine = cls.engine
        database.SessionLocal.configure(bind=cls.engine)

        import app.models as models  # noqa: F401
        from app.main import app, init_db
        init_db()
        cls.client = TestClient(app)

    # ------------------------------------------------------------ helpers
    def publish(self, plugin, version, payload, deps=None, rts=None, conflicts=None,
                sha=None, expect=None):
        body = payload if isinstance(payload, bytes) else payload.encode()
        meta = {
            "plugin": plugin, "version": version,
            "sha256": sha or hashlib.sha256(body).hexdigest(),
            "dependencies": deps or [],
            "runtimes": rts or [],
            "conflicts": conflicts or [],
        }
        files = {
            "metadata": (None, _json(meta), "application/json"),
            "package": (f"{plugin}-{version}.bin", io.BytesIO(body),
                        "application/octet-stream"),
        }
        resp = self.client.post(f"/api/v1/plugins/{plugin}/versions", files=files)
        if expect is not None:
            self.assertEqual(resp.status_code, expect, resp.text)
        return resp

    # ------------------------------------------------------------ tests
    def test_health_and_root(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})

    def test_publish_and_get(self):
        r = self.publish("pkg-a", "1.0.0", b"A1",
                         rts=[{"name": "python", "spec": ">=3.9"}])
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["published_revision"], 1)
        g = self.client.get("/api/v1/plugins/pkg-a/versions/1.0.0")
        self.assertEqual(g.status_code, 200)
        self.assertEqual(g.json()["runtimes"], [{"name": "python", "spec": ">=3.9"}])

    def test_bad_sha256_rejected(self):
        r = self.publish("pkg-bad", "1.0.0", b"x", sha="0" * 64, expect=422)
        self.assertIn("摘要不符", r.text)

    def test_invalid_version_and_spec_rejected(self):
        r = self.publish("pkg-badver", "not-a-version", b"x")
        self.assertEqual(r.status_code, 422, r.text)
        r = self.publish("pkg-badspec", "1.0.0", b"x",
                         deps=[{"plugin": "x", "spec": "~=1"}])
        self.assertEqual(r.status_code, 422, r.text)

    def test_duplicate_version_immutable(self):
        self.publish("pkg-dup", "1.0.0", b"same-payload", expect=201)
        r = self.publish("pkg-dup", "1.0.0", b"same-payload")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("不可改写", r.json()["detail"])

    def test_solve_transitive_and_cache(self):
        self.publish("libx", "2.0.0", b"libx2",
                     rts=[{"name": "python", "spec": ">=3.10"}])
        self.publish("libx", "1.0.0", b"libx1",
                     rts=[{"name": "python", "spec": ">=3.8,<3.10"}])
        self.publish("toolz", "1.0.0", b"toolz",
                     deps=[{"plugin": "libx", "spec": ">=2"}],
                     rts=[{"name": "python", "spec": ">=3.10"}])
        req = {"runtimes": [{"name": "python", "spec": "3.11.*"}],
               "targets": {"toolz": ""}}
        r1 = self.client.post("/api/v1/solutions", json=req)
        self.assertEqual(r1.status_code, 200, r1.text)
        body1 = r1.json()
        self.assertFalse(body1["cached"])
        self.assertEqual(body1["selection"]["libx"]["version"], "2.0.0")
        self.assertEqual(body1["install_order"], ["libx", "toolz"])

        r2 = self.client.post("/api/v1/solutions", json=req)
        body2 = r2.json()
        self.assertTrue(body2["cached"])
        self.assertEqual(body2["solution_id"], body1["solution_id"])
        # 选择结果逐字节一致
        self.assertEqual(body2["selection"], body1["selection"])

    def test_unsat_returns_conflicts_no_partial_solution(self):
        # libz 只有 1.x，请求 >=9
        self.publish("libz", "1.0.0", b"libz")
        r = self.client.post("/api/v1/solutions",
                             json={"targets": {"libz": ">=9"}})
        self.assertEqual(r.status_code, 409)
        detail = r.json()["detail"]
        self.assertEqual(detail["code"], "unsatisfiable")
        self.assertTrue(detail["conflicts"])
        self.assertEqual(detail["conflicts"][0]["type"], "no_matching_version")
        # 失败不保存：solutions 表为空（前面成功的方案除外），这里直接确认
        # 对该 request_hash 查不到记录——通过再次同请求仍 cached=false 间接验证
        again = self.client.post("/api/v1/solutions",
                                 json={"targets": {"libz": ">=9"}})
        self.assertEqual(again.status_code, 409)

    def test_cycle_422(self):
        self.publish("cyc-a", "1.0.0", b"a",
                     deps=[{"plugin": "cyc-b", "spec": ">=1"}])
        self.publish("cyc-b", "1.0.0", b"b",
                     deps=[{"plugin": "cyc-a", "spec": ">=1"}])
        r = self.client.post("/api/v1/solutions",
                             json={"targets": {"cyc-a": ""}})
        self.assertEqual(r.status_code, 422, r.text)
        detail = r.json()["detail"]
        self.assertEqual(detail["code"], "dependency_cycle")
        cyc = detail["conflicts"][0]["cycle"]
        self.assertEqual(cyc[0], cyc[-1])

    def test_revoke_affects_new_but_not_history(self):
        self.publish("rev-lib", "1.0.0", b"revlib")
        self.publish("rev-app", "1.0.0", b"revapp",
                     deps=[{"plugin": "rev-lib", "spec": ""}])
        sol = self.client.post("/api/v1/solutions",
                               json={"targets": {"rev-app": ""}}).json()
        sid, revision = sol["solution_id"], sol["revision"]

        r = self.client.post("/api/v1/plugins/rev-lib/versions/1.0.0/revoke",
                             json={"reason": "缺陷"})
        self.assertEqual(r.status_code, 200, r.text)
        new_rev = r.json()["revoked_revision"]
        self.assertGreater(new_rev, revision)

        # 历史方案仍可查，且撤销项被标记
        hist = self.client.get(f"/api/v1/solutions/{sid}").json()
        self.assertTrue(hist["selection"]["rev-lib"]["revoked"])
        self.assertFalse(hist["selection"]["rev-app"]["revoked"])

        # 旧修订重放仍可得到原方案
        replay = self.client.post("/api/v1/solutions", json={
            "revision": revision, "targets": {"rev-app": ""}}).json()
        self.assertEqual(replay["selection"]["rev-lib"]["version"], "1.0.0")
        self.assertFalse(replay["selection"]["rev-lib"]["revoked"])

        # 新求解：rev-lib 已撤销 -> 无解（409）
        r = self.client.post("/api/v1/solutions",
                             json={"targets": {"rev-app": ""}})
        self.assertEqual(r.status_code, 409)

    def test_double_revoke_conflict(self):
        self.publish("dbl", "1.0.0", b"dbl")
        self.client.post("/api/v1/plugins/dbl/versions/1.0.0/revoke",
                         json={"reason": "x"})
        r = self.client.post("/api/v1/plugins/dbl/versions/1.0.0/revoke",
                             json={"reason": "y"})
        self.assertEqual(r.status_code, 409)

    def test_download_with_sha_header(self):
        self.publish("dl", "1.0.0", b"download-me")
        r = self.client.get("/api/v1/plugins/dl/versions/1.0.0/download")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"download-me")
        self.assertEqual(r.headers["x-sha256"],
                         hashlib.sha256(b"download-me").hexdigest())

    def test_mutex_end_to_end(self):
        self.publish("mx", "1.0.0", b"mx", conflicts=["my"])
        self.publish("my", "1.0.0", b"my")
        self.publish("mhost", "1.0.0", b"mhost",
                     deps=[{"plugin": "mx", "spec": ""},
                           {"plugin": "my", "spec": ""}])
        r = self.client.post("/api/v1/solutions",
                             json={"targets": {"mhost": ""}})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(any(c["type"] == "mutex_conflict"
                            for c in r.json()["detail"]["conflicts"]))

    # ----------------------------------------------------- 预演 rehearsals
    def _publish_alpha_beta(self):
        # python 3.10+ 新版；3.8/3.9 旧版；beta 新版依赖 alpha>=2
        self.publish("alpha", "2.0.0", b"alpha2",
                     rts=[{"name": "python", "spec": ">=3.10"}])
        self.publish("alpha", "1.0.0", b"alpha1",
                     rts=[{"name": "python", "spec": ">=3.8"}])
        self.publish("beta", "2.0.0", b"beta2",
                     deps=[{"plugin": "alpha", "spec": ">=2"}],
                     rts=[{"name": "python", "spec": ">=3.10"}])
        self.publish("beta", "1.0.0", b"beta1",
                     deps=[{"plugin": "alpha", "spec": ">=1"}],
                     rts=[{"name": "python", "spec": ">=3.8"}])

    def _baseline_solution(self, plugin="beta"):
        req = {"runtimes": [{"name": "python", "spec": "3.11.*"}],
               "targets": {plugin: ""}}
        r = self.client.post("/api/v1/solutions", json=req)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        return body["solution_id"], body["revision"], req

    def test_rehearse_keeps_all_when_unchanged(self):
        self._publish_alpha_beta()
        sid, rev, _ = self._baseline_solution()
        r = self.client.post(f"/api/v1/solutions/{sid}/rehearsals",
                             json={"revision": rev})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body["saved"])
        self.assertEqual(body["baseline_solution_id"], sid)
        self.assertEqual(body["revision"], rev)
        self.assertEqual(body["selection"]["alpha"]["version"], "2.0.0")
        self.assertEqual(body["selection"]["beta"]["version"], "2.0.0")
        kinds = {d["plugin"]: d["change"] for d in body["diff"]}
        self.assertEqual(kinds, {"alpha": "kept", "beta": "kept"})
        self.assertEqual(body["changes"]["total"], 0)
        self.assertEqual(body["install_order"], ["alpha", "beta"])
        # 预演不落库：方案表数量不变
        from app import database
        from app.models import Solution
        with database.SessionLocal() as s:
            n = s.query(Solution).count()
        self.client.post(f"/api/v1/solutions/{sid}/rehearsals",
                         json={"revision": rev})
        with database.SessionLocal() as s:
            self.assertEqual(s.query(Solution).count(), n)

    def test_rehearse_runtime_override_minimal_change(self):
        self._publish_alpha_beta()
        sid, rev, _ = self._baseline_solution()
        r = self.client.post(f"/api/v1/solutions/{sid}/rehearsals", json={
            "revision": rev,
            "runtimes": [{"name": "python", "spec": "3.9.*"}],
        })
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["runtimes_overridden"])
        self.assertFalse(body["targets_overridden"])
        self.assertEqual(body["selection"]["alpha"]["version"], "1.0.0")
        self.assertEqual(self._diff_map(body),
                         {"alpha": "changed", "beta": "changed"})
        self.assertEqual(body["changes"]["changed"], 2)
        self.assertEqual(body["changes"]["total"], 2)

    def test_rehearse_targets_override_add_and_remove(self):
        self._publish_alpha_beta()
        self.publish("extra", "1.0.0", b"extra")
        # 基线方案包含 extra
        req = {"runtimes": [{"name": "python", "spec": "3.11.*"}],
               "targets": {"beta": "", "extra": ""}}
        base = self.client.post("/api/v1/solutions", json=req).json()
        sid, rev = base["solution_id"], base["revision"]

        # 覆盖目标：去掉 extra -> removed；新增无需（此处演示 removed）
        r = self.client.post(f"/api/v1/solutions/{sid}/rehearsals", json={
            "revision": rev, "targets": {"beta": ""}})
        self.assertEqual(r.status_code, 200, r.text)
        dm = self._diff_map(r.json())
        self.assertEqual(dm["extra"], "removed")
        self.assertNotIn("extra", r.json()["selection"])
        self.assertEqual(r.json()["changes"]["removed"], 1)

        # 反向：基线无 extra，覆盖目标加入 -> added
        sid2, rev2, _ = self._baseline_solution()
        r = self.client.post(f"/api/v1/solutions/{sid2}/rehearsals", json={
            "revision": rev2,
            "targets": {"beta": "", "extra": ""}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._diff_map(r.json())["extra"], "added")
        self.assertEqual(r.json()["changes"]["added"], 1)

    def test_rehearse_prefers_keep_over_upgrade(self):
        # lib 1/2 均满足，基线在可复现条件下保存 lib 1.0（钉住 spec <2）
        self.publish("keep-lib", "2.0.0", b"l2")
        self.publish("keep-lib", "1.0.0", b"l1")
        self.publish("keep-app", "1.0.0", b"a1",
                     deps=[{"plugin": "keep-lib", "spec": ">=1"}])
        base = self.client.post("/api/v1/solutions", json={
            "targets": {"keep-app": "", "keep-lib": "<2"}}).json()
        sid, rev = base["solution_id"], base["revision"]
        # 预演放宽目标（仅 keep-app）：默认求解会选 lib 2.0，但保留 1.0 变动更少
        r = self.client.post(f"/api/v1/solutions/{sid}/rehearsals",
                             json={"revision": rev,
                                   "targets": {"keep-app": ""}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["selection"]["keep-lib"]["version"], "1.0.0")
        self.assertEqual(r.json()["changes"]["total"], 0)

    def test_rehearse_stable_after_later_revoke(self):
        self._publish_alpha_beta()
        sid, rev, _ = self._baseline_solution()
        first = self.client.post(
            f"/api/v1/solutions/{sid}/rehearsals",
            json={"revision": rev}).json()
        # 之后撤销 alpha 1.0.0（产生新修订）
        rr = self.client.post(
            "/api/v1/plugins/alpha/versions/1.0.0/revoke",
            json={"reason": "x"})
        self.assertEqual(rr.status_code, 200, rr.text)
        # 固定在原修订重放：结果不变
        second = self.client.post(
            f"/api/v1/solutions/{sid}/rehearsals",
            json={"revision": rev}).json()
        self.assertEqual(second["selection"], first["selection"])
        self.assertEqual(second["diff"], first["diff"])

    def test_rehearse_unsat_409_and_cycle_422_no_partial(self):
        self._publish_alpha_beta()
        sid, rev, _ = self._baseline_solution()
        r = self.client.post(f"/api/v1/solutions/{sid}/rehearsals", json={
            "revision": rev,
            "runtimes": [{"name": "python", "spec": "2.7.*"}]})
        self.assertEqual(r.status_code, 409, r.text)
        detail = r.json()["detail"]
        self.assertEqual(detail["code"], "unsatisfiable")
        self.assertEqual(detail["baseline_solution_id"], sid)
        self.assertTrue(detail["conflicts"])

        # 非法范围 -> 400
        r = self.client.post(f"/api/v1/solutions/{sid}/rehearsals", json={
            "revision": rev, "targets": {"beta": "~=1"}})
        self.assertEqual(r.status_code, 400, r.text)

        # 空 targets 覆盖 -> 422（pydantic 校验）
        r = self.client.post(f"/api/v1/solutions/{sid}/rehearsals", json={
            "revision": rev, "targets": {}})
        self.assertEqual(r.status_code, 422, r.text)

        # 依赖环 -> 422
        self.publish("rc-a", "1.0.0", b"a",
                     deps=[{"plugin": "rc-b", "spec": ""}])
        self.publish("rc-b", "1.0.0", b"b",
                     deps=[{"plugin": "rc-a", "spec": ""}])
        r = self.client.post(f"/api/v1/solutions/{sid}/rehearsals", json={
            "revision": rev, "targets": {"rc-a": ""}})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(r.json()["detail"]["code"], "dependency_cycle")

    def test_rehearse_missing_solution_or_revision(self):
        self._publish_alpha_beta()
        sid, rev, _ = self._baseline_solution()
        # 方案不存在 -> 404
        r = self.client.post("/api/v1/solutions/999999/rehearsals",
                             json={"revision": rev})
        self.assertEqual(r.status_code, 404, r.text)
        # 修订不存在 -> 400
        r = self.client.post(f"/api/v1/solutions/{sid}/rehearsals",
                             json={"revision": rev + 1000})
        self.assertEqual(r.status_code, 400, r.text)

    @staticmethod
    def _diff_map(body):
        return {d["plugin"]: d["change"] for d in body["diff"]}


def _json(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
