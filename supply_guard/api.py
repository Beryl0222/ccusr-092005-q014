"""HTTP API（标准库 http.server，零额外依赖）。

路由：
  POST /api/reports                      企业上报（DAILY/CORRECTION）
  GET  /api/reports?date=YYYY-MM-DD      企业查看本企业版本链
  POST /api/qc-events                    检验放行/不合格/延期
  POST /api/demands                      区域提需求
  POST /api/demands/{id}/cancel          运输取消
  POST /api/suggestions                  计算建议 {demand_id}（不锁定）
  POST /api/commitments/approve          监管确认锁定
  POST /api/commitments/{id}/fulfill     登记履行
  POST /api/commitments/{id}/release     释放未履行部分
  GET  /api/commitments                  承诺台账（按角色隔离）
  GET  /api/atp?medicine_id=..[&region_id=..]
  GET  /api/board?medicine_id=..         跨企业态势（仅监管）
  GET  /api/batches/{id}                 批次追溯

鉴权：Authorization: Bearer <token>
幂等：Idempotency-Key 请求头（写接口）
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .errors import ServiceError
from .seed import load_seed, seed_store
from .service import Service
from .store import Store


class ApiHandler(BaseHTTPRequestHandler):
    service: Service  # 由 factory 注入到类属性

    server_version = "SupplyGuard/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静
        return

    # ---------------------------------------------------------------- #
    def _send(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, exc: ServiceError) -> None:
        self._send(exc.http_status, {"error": type(exc).__name__, "message": str(exc)})

    def _actor(self):
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        return self.service.authenticate(token)

    def _read_json(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ServiceError(f"请求体不是合法 JSON：{exc}") from None
        if not isinstance(data, dict):
            raise ServiceError("请求体必须是 JSON 对象")
        return data

    def _idem(self) -> str | None:
        return self.headers.get("Idempotency-Key")

    # ---------------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        try:
            parts = [p for p in urlsplit(self.path).path.split("/") if p]
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            actor = self._actor()
            if parts == ["api", "reports"]:
                date = q.get("date")
                if not date:
                    raise ServiceError("date 查询参数必填")
                return self._send(200, {"versions": self.service.get_report_versions(actor, date)})
            if parts == ["api", "commitments"]:
                return self._send(200, {"commitments": self.service.list_commitments(
                    actor, demand_id=q.get("demand_id"), enterprise_id=q.get("enterprise_id"))})
            if parts == ["api", "atp"]:
                if "medicine_id" not in q:
                    raise ServiceError("medicine_id 必填")
                return self._send(200, self.service.atp_overview(
                    actor, q["medicine_id"], region_id=q.get("region_id")))
            if parts == ["api", "board"]:
                return self._send(200, self.service.situation_board(
                    actor, q.get("medicine_id")))
            if len(parts) == 3 and parts[:2] == ["api", "batches"]:
                return self._send(200, self.service.batch_trace(actor, parts[2]))
            self._send(404, {"error": "NotFound", "message": "未知路径"})
        except ServiceError as exc:
            self._error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            parts = [p for p in urlsplit(self.path).path.split("/") if p]
            actor = self._actor()
            body = self._read_json()
            svc = self.service

            if parts == ["api", "reports"]:
                return self._send(201, svc.submit_report(actor, body, idem_key=self._idem()))
            if parts == ["api", "qc-events"]:
                return self._send(201, svc.record_qc_event(actor, body, idem_key=self._idem()))
            if parts == ["api", "demands"]:
                return self._send(201, svc.create_demand(actor, body, idem_key=self._idem()))
            if len(parts) == 4 and parts[:2] == ["api", "demands"] and parts[3] == "cancel":
                return self._send(200, svc.cancel_demand(actor, parts[2],
                                                         idem_key=self._idem()))
            if parts == ["api", "suggestions"]:
                if "demand_id" not in body:
                    raise ServiceError("demand_id 必填")
                return self._send(201, svc.compute_suggestion(
                    actor, body["demand_id"], at_ts=body.get("at_ts")))
            if parts == ["api", "commitments", "approve"]:
                return self._send(201, svc.approve_commitment(
                    actor, body, idem_key=self._idem(), at_ts=body.get("at_ts")))
            if len(parts) == 4 and parts[:2] == ["api", "commitments"] and parts[3] == "fulfill":
                body.setdefault("commitment_id", parts[2])
                return self._send(200, svc.fulfill_commitment(
                    actor, body, idem_key=self._idem()))
            if len(parts) == 4 and parts[:2] == ["api", "commitments"] and parts[3] == "release":
                body.setdefault("commitment_id", parts[2])
                return self._send(200, svc.release_commitment(
                    actor, body, idem_key=self._idem()))
            self._send(404, {"error": "NotFound", "message": "未知路径"})
        except ServiceError as exc:
            self._error(exc)


def build_service(db_path: str = ":memory:", seed_path: str | None = "fixtures/seed.json") -> Service:
    store = Store(db_path)
    service = Service(store)
    if seed_path:
        seed_store(store, load_seed(seed_path), service=service)
    return service


def create_server(host: str, port: int, service: Service) -> ThreadingHTTPServer:
    handler = ApiHandler
    handler.service = service
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="重点药品生产监测与调配后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="supply_guard.db")
    parser.add_argument("--seed", default="fixtures/seed.json")
    args = parser.parse_args()

    service = build_service(args.db, args.seed)
    httpd = create_server(args.host, args.port, service)
    print(f"Supply Guard API 监听 http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
