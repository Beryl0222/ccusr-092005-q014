"""HTTP JSON API（标准库实现，无第三方依赖）。

鉴权：所有业务接口要求 ``X-User-Id`` 头；企业/监管两种角色的边界在
服务层强制，这里只做异常 -> 状态码映射与 JSON 编解码。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import (
    Conflict,
    ConstraintViolation,
    NotFound,
    OvercommitError,
    PermissionDenied,
    SupplyError,
    ValidationError,
)
from .platform import SupplyPlatform

_STATUS = {
    ValidationError: 400,
    ConstraintViolation: 422,
    OvercommitError: 409,
    Conflict: 409,
    PermissionDenied: 403,
    NotFound: 404,
}


def _json_default(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class Handler(BaseHTTPRequestHandler):
    server_version = "SupplyGuard/1.0"

    # -- 工具 -------------------------------------------------------------

    def _platform(self) -> SupplyPlatform:
        return self.server.platform  # type: ignore[attr-defined]

    def _actor(self) -> dict:
        user_id = self.headers.get("X-User-Id")
        if not user_id:
            raise PermissionDenied("缺少 X-User-Id 头")
        return self._platform().actor(user_id)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError(f"请求体不是合法 JSON：{exc}")
        if not isinstance(data, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return data

    def _send(self, status: int, payload) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=_json_default).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, exc: Exception) -> None:
        status = _STATUS.get(type(exc), 500)
        if isinstance(exc, SupplyError) and status == 500:
            # 未登记的领域异常按规则违反处理，避免暴露 500 掩盖问题。
            status = 422
        self._send(status, {"error": type(exc).__name__, "message": str(exc)})

    def log_message(self, fmt, *args) -> None:  # 静默：由调用方决定日志
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    # -- 路由 -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = parse_qs(parsed.query)
            if path == "/health":
                self._send(200, {"status": "ok"})
                return
            actor = self._actor()
            p = self._platform()

            if path == "/reports":
                key = qs.get("report_key", [None])[0]
                rid = qs.get("id", [None])[0]
                if rid:
                    self._send(200, p.reporting.get_report(rid, actor=actor))
                else:
                    self._send(200, p.reporting.list_reports(actor=actor, report_key=key))
            elif path == "/requests":
                status = qs.get("status", [None])[0]
                self._send(200, p.requests.list_requests(actor=actor, status=status))
            elif path == "/dashboard/enterprise":
                self._send(
                    200,
                    p.views.enterprise_dashboard(actor["enterprise_id"], actor=actor),
                )
            elif path == "/dashboard/regulator":
                self._send(200, p.views.regulator_overview(actor=actor))
            elif path.startswith("/batches/"):
                self._send(200, p.production.get_batch(path.split("/")[-1], actor=actor))
            elif path.startswith("/commitments/"):
                tail = path.split("/")
                cid = tail[2]
                if len(tail) == 4 and tail[3] == "trace":
                    self._send(200, p.views.commitment_trace(cid, actor=actor))
                else:
                    self._send(200, p.fulfillment.get_commitment(cid, actor=actor))
            else:
                self._send(404, {"error": "NotFound", "message": f"无此路径：{path}"})
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            body = self._body()
            actor = self._actor()
            p = self._platform()

            if path == "/batches":
                self._send(201, p.production.register_batch(body, actor=actor))
            elif path == "/material-supplies":
                self._send(
                    201, p.production.report_material_supply(body, actor=actor)
                )
            elif path.startswith("/batches/") and path.endswith("/qc"):
                batch_id = path.split("/")[2]
                self._send(200, p.production.record_qc(batch_id, body, actor=actor))
            elif path == "/reports":
                self._send(201, p.reporting.submit_report(body, actor=actor))
            elif path == "/requests":
                self._send(201, p.requests.create_request(body, actor=actor))
            elif path == "/allocations/plan":
                self._send(
                    200,
                    p.allocations.build_plan(
                        actor=actor, medicine_id=body.get("medicine_id"),
                        plan_ts=body.get("plan_ts"),
                    ),
                )
            elif path == "/decisions/confirm":
                self._send(200, p.allocations.confirm(body, actor=actor))
            elif path.startswith("/commitments/") and path.endswith("/events"):
                cid = path.split("/")[2]
                self._send(200, p.fulfillment.record_event(cid, body, actor=actor))
            else:
                self._send(404, {"error": "NotFound", "message": f"无此路径：{path}"})
        except Exception as exc:  # noqa: BLE001
            self._error(exc)


def create_server(
    platform: SupplyPlatform, host: str = "127.0.0.1", port: int = 8080,
    *, verbose: bool = False,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.platform = platform  # type: ignore[attr-defined]
    server.verbose = verbose  # type: ignore[attr-defined]
    return server


def serve_in_thread(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread
