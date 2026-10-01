#!/usr/bin/env python3
"""纯标准库调用示例（离线环境无需 pip install requests）。

运行：
    python3 examples/api_client.py --base http://localhost:8080

展示：发布（含 SHA-256 校验）、求解、缓存命中、无解冲突明细、
撤销、历史方案与撤销标记、按旧修订重放。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import urllib.error
import urllib.request
import uuid


class Client:
    def __init__(self, base: str, token: str | None = None):
        self.base = base.rstrip("/") + "/api/v1"
        self.token = token

    def _request(self, method: str, path: str, data=None, headers=None):
        url = self.base + path
        body = None
        hdrs = dict(headers or {})
        if data is not None and not isinstance(data, (bytes, bytearray)):
            body = json.dumps(data).encode()
            hdrs.setdefault("Content-Type", "application/json")
        else:
            body = data
        if self.token:
            hdrs["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return resp.status, dict(resp.headers), (
                    json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = raw.decode("utf-8", "replace")
            return e.code, dict(e.headers), payload

    # ------------------------------------------------------------ API
    def publish(self, meta: dict, payload: bytes):
        boundary = "----lab" + uuid.uuid4().hex
        buf = io.BytesIO()
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(
            b'Content-Disposition: form-data; name="metadata"\r\n'
            b"Content-Type: application/json\r\n\r\n")
        buf.write(json.dumps(meta, ensure_ascii=False).encode())
        filename = f"{meta['plugin']}-{meta['version']}.bin"
        buf.write(f"\r\n--{boundary}\r\n".encode())
        buf.write(
            f'Content-Disposition: form-data; name="package"; filename="{filename}"\r\n'
            .encode())
        buf.write(b"Content-Type: application/octet-stream\r\n\r\n")
        buf.write(payload)
        buf.write(f"\r\n--{boundary}--\r\n".encode())
        return self._request(
            "POST", f"/plugins/{meta['plugin']}/versions",
            data=buf.getvalue(),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})

    def solve(self, req: dict):
        return self._request("POST", "/solutions", data=req)

    def solution(self, sid: int):
        return self._request("GET", f"/solutions/{sid}")

    def rehearse(self, sid: int, req: dict):
        """基于已保存方案 sid 在目标修订上预演（不落库）。"""
        return self._request("POST", f"/solutions/{sid}/rehearsals", data=req)

    def revoke(self, plugin: str, version: str, reason: str):
        return self._request(
            "POST", f"/plugins/{plugin}/versions/{version}/revoke",
            data={"reason": reason})

    def revisions(self):
        return self._request("GET", "/revisions")


def show(title, status, payload):
    print(f"\n=== {title} [HTTP {status}] ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--token", default=None)
    args = ap.parse_args()
    c = Client(args.base, args.token)

    def pub(plugin, version, runtimes, deps=None, conflicts=None, content=None):
        content = (content or f"{plugin}-{version}").encode()
        meta = {
            "plugin": plugin,
            "version": version,
            "sha256": hashlib.sha256(content).hexdigest(),
            "runtimes": runtimes,
            "dependencies": deps or [],
            "conflicts": conflicts or [],
        }
        st, _, body = c.publish(meta, content)
        show(f"发布 {plugin}=={version}", st, body if st < 400 else body)
        return st

    # 准备数据：A、B 两个插件，B 依赖 A
    pub("alpha", "2.0.0", [{"name": "python", "spec": ">=3.10"}],
        content="alpha2")
    pub("alpha", "1.5.0", [{"name": "python", "spec": ">=3.8,<3.10"}],
        content="alpha1")
    pub("beta", "1.0.0",
        [{"name": "python", "spec": ">=3.10"}],
        deps=[{"plugin": "alpha", "spec": ">=2,<3"}], content="beta1")

    # 求解：Python 3.11
    req = {"runtimes": [{"name": "python", "spec": "3.11.*"}],
           "targets": {"beta": ""}}
    st, _, sol = c.solve(req)
    show("求解 beta（Python 3.11）", st, sol)
    assert st == 200 and sol["selection"]["alpha"]["version"] == "2.0.0"
    assert sol["install_order"] == ["alpha", "beta"]
    sid = sol["solution_id"]

    # 同请求再发一次 -> 缓存
    st, _, sol2 = c.solve(req)
    show("重复请求（cached 应为 true）", st, {"cached": sol2["cached"]})
    assert sol2["cached"] is True
    assert sol2["selection"] == sol["selection"]

    # Python 3.9 -> beta 1.0.0 不兼容，整体无解
    st, _, err = c.solve({
        "runtimes": [{"name": "python", "spec": "3.9.*"}],
        "targets": {"beta": ""}})
    show("无解：beta 不支持 3.9（409 + 冲突）", st, err)
    assert st == 409 and err["detail"]["code"] == "unsatisfiable"

    # 撤销 alpha 2.0.0
    st, _, rev = c.revoke("alpha", "2.0.0", "回归缺陷")
    show("撤销 alpha==2.0.0", st, {"revoked_revision": rev["revoked_revision"]})

    # 历史方案仍可查，且 alpha 被标 revoked
    st, _, hist = c.solution(sid)
    show(f"历史方案 {sid}（alpha 应 revoked=true）", st, {
        "alpha_revoked": hist["selection"]["alpha"]["revoked"],
        "selection": hist["selection"],
    })
    assert hist["selection"]["alpha"]["revoked"] is True

    # 按旧修订重放：撤销后旧修订仍可得到原方案
    old_rev = sol["revision"]
    st, _, replay = c.solve({"revision": old_rev, **req})
    show(f"在旧修订 {old_rev} 重放", st,
         {"revision": replay["revision"], "alpha": replay["selection"]["alpha"]["version"]})
    assert replay["selection"]["alpha"]["version"] == "2.0.0"

    # 预演：基于已保存方案 sid，在旧修订上把运行环境整体覆盖为 3.9
    # （最小变动优先：alpha/beta 需一起换版；结果不保存）
    st, _, preview = c.rehearse(sid, {
        "revision": old_rev,
        "runtimes": [{"name": "python", "spec": "3.9.*"}]})
    show(f"预演方案 {sid} @修订{old_rev}（覆盖 python 3.9）", st, {
        "saved": preview["saved"],
        "selection": {k: v["version"] for k, v in preview["selection"].items()},
        "changes": preview["changes"],
        "diff": preview["diff"],
    })
    assert st == 200 and preview["saved"] is False
    assert preview["selection"]["alpha"]["version"] == "1.5.0"
    assert preview["selection"]["beta"]["version"] == "1.0.0"
    assert preview["changes"]["changed"] == 2
    # 预演不产生新方案：原 sid 仍可查且内容不变
    st, _, same = c.solution(sid)
    assert same["selection"]["alpha"]["version"] == "2.0.0"

    # 预演无解：409 + 结构化原因（不保存部分方案）
    st, _, perr = c.rehearse(sid, {
        "revision": old_rev,
        "runtimes": [{"name": "python", "spec": "2.7.*"}]})
    show("预演无解（409 + conflicts，不落库）", st, perr)
    assert st == 409 and perr["detail"]["code"] == "unsatisfiable"

    # 预演入参非法：方案不存在 404 / 修订不存在 400 / 非法范围 400
    st, _, _ = c.rehearse(999999, {"revision": old_rev})
    assert st == 404, st
    st, _, _ = c.rehearse(sid, {"revision": 999999})
    assert st == 400, st
    st, _, _ = c.rehearse(sid, {"revision": old_rev,
                                "targets": {"beta": "~=1"}})
    assert st == 400, st

    # 摘要不符必须拒绝
    bad = {"plugin": "alpha", "version": "9.9.9", "sha256": "0" * 64,
           "runtimes": [], "dependencies": [], "conflicts": []}
    st, _, body = c.publish(bad, b"different-content")
    show("摘要不符（应 422 拒绝）", st, body)
    assert st == 422

    print("\n全部示例断言通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
