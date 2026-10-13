"""A mock Superset 4.0 API, used to verify the client end to end.

It implements only what the CLI calls, but it *enforces* the contract rather
than rubber-stamping it: CSRF header and Referer are required on mutating
requests, bearer tokens are checked, and ``/api/v1/sqllab/execute/`` asserts the
statement arrived with a LIMIT and without write keywords. That way an e2e run
proves the guard's output actually reaches the server in the expected shape.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ACCESS_TTL = 300
VALID_USER = "agent-test"
VALID_PASSWORD = "test-password"
CSRF_TOKEN = "csrf-token-abc123"

WRITE_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|create|alter|truncate|grant|merge|copy)\b", re.IGNORECASE
)

DATABASES = [
    {"id": 3, "database_name": "acme_oms", "backend": "postgresql",
     "expose_in_sqllab": True, "allow_dml": False,
     "sqlalchemy_uri": "postgresql://tathya:sup3rs3cret@replica.internal:5432/acme_oms"},
    {"id": 4, "database_name": "examples", "backend": "postgresql",
     "expose_in_sqllab": True, "allow_dml": False,
     "sqlalchemy_uri": "postgresql://x:y@localhost/examples"},
]
DASHBOARDS = [
    {"id": 1, "dashboard_title": "Fulfillment Ops", "status": "published",
     "changed_on_delta_humanized": "2 days ago", "position_json": "", "slug": "fulfil-ops"},
    {"id": 2, "dashboard_title": "Order Health", "status": "draft",
     "changed_on_delta_humanized": "1 hour ago", "position_json": "", "slug": None},
]
CHARTS = [
    {"id": 10, "slice_name": "Stuck Orders", "viz_type": "big_number_total", "datasource_id": 7,
     "changed_on_delta_humanized": "3 hours ago", "uuid": "uuid-10", "dashboards": [{"id": 1}]},
    {"id": 11, "slice_name": "Orders by Status", "viz_type": "table", "datasource_id": 7,
     "changed_on_delta_humanized": "5 days ago", "uuid": "uuid-11", "dashboards": []},
    # Sits on dataset 8 -> the `examples` database, which tests do NOT allowlist.
    {"id": 12, "slice_name": "Other Client Chart", "viz_type": "table", "datasource_id": 8,
     "changed_on_delta_humanized": "1 day ago", "uuid": "uuid-12", "dashboards": []},
    # No datasource at all - the database cannot be determined.
    {"id": 13, "slice_name": "Orphan Chart", "viz_type": "table", "datasource_id": None,
     "changed_on_delta_humanized": "1 day ago", "uuid": "uuid-13", "dashboards": []},
]
#: Alerts & Reports fixture. `last_eval_dttm` values are placeholders replaced by
#: the tests, which pin a clock because staleness is schedule-relative.
REPORTS = [
    {"id": 1, "name": "Monthly rollup", "type": "Report", "active": True,
     "last_state": "Success", "crontab": "0 0 1 * *", "crontab_humanized": "monthly",
     "timezone": "UTC", "last_eval_dttm": None, "description": ""},
    {"id": 2, "name": "Daily 9am overdue", "type": "Report", "active": True,
     "last_state": "Success", "crontab": "0 9 * * *", "crontab_humanized": "daily 9am",
     "timezone": "UTC", "last_eval_dttm": None, "description": ""},
    {"id": 3, "name": "Daily 9am fresh", "type": "Alert", "active": True,
     "last_state": "Not triggered", "crontab": "0 9 * * *", "crontab_humanized": "daily 9am",
     "timezone": "UTC", "last_eval_dttm": None, "description": ""},
    {"id": 4, "name": "Broken alert", "type": "Alert", "active": True,
     "last_state": "Error", "crontab": "0 9 * * *", "crontab_humanized": "daily 9am",
     "timezone": "UTC", "last_eval_dttm": None, "description": ""},
    {"id": 5, "name": "Switched off", "type": "Report", "active": False,
     "last_state": "Success", "crontab": "0 9 * * *", "crontab_humanized": "daily 9am",
     "timezone": "UTC", "last_eval_dttm": None, "description": ""},
    {"id": 6, "name": "Never ran", "type": "Alert", "active": True,
     "last_state": None, "crontab": "0 9 * * *", "crontab_humanized": "daily 9am",
     "timezone": "UTC", "last_eval_dttm": None, "description": ""},
]

DATASETS = [
    {"id": 7, "table_name": "order_header", "schema": "public", "kind": "physical",
     "database": {"database_name": "acme_oms"},
     "columns": [{"column_name": "order_id", "type": "VARCHAR", "is_dttm": False},
                 {"column_name": "status_id", "type": "VARCHAR", "is_dttm": False},
                 {"column_name": "created_stamp", "type": "TIMESTAMP", "is_dttm": True}],
     "metrics": [{"metric_name": "count"}], "sql": None},
    {"id": 8, "table_name": "other_table", "schema": "public", "kind": "physical",
     "database": {"database_name": "examples"},
     "columns": [{"column_name": "x", "type": "VARCHAR", "is_dttm": False}],
     "metrics": [], "sql": None},
    # A virtual (SQL) dataset on the allowlisted database, for dataset writes.
    {"id": 9, "table_name": "stuck_orders_v", "schema": "public", "kind": "virtual",
     "database": {"database_name": "acme_oms", "id": 1},
     "columns": [{"column_name": "order_id", "type": "VARCHAR", "is_dttm": False}],
     "metrics": [], "sql": "SELECT order_id FROM order_header WHERE status_id = 'APPROVED'"},
    # A virtual dataset on `examples`, which tests do NOT allowlist.
    {"id": 10, "table_name": "other_v", "schema": "public", "kind": "virtual",
     "database": {"database_name": "examples", "id": 2},
     "columns": [{"column_name": "x", "type": "VARCHAR", "is_dttm": False}],
     "metrics": [], "sql": "SELECT x FROM other_table"},
]

def _q(qid, db, status, start_ms, seconds, user, rows=1, sql="SELECT 1"):
    return {"id": qid, "status": status, "rows": rows, "sql": sql, "executed_sql": sql,
            "sql_tables": [], "schema": "public", "tab_name": "t",
            "start_time": start_ms, "end_time": start_ms + seconds * 1000,
            "database": {"database_name": db},
            "user": {"id": 1, "first_name": user, "last_name": "Tester"}}


#: Query history fixture. Times are filled in per request so the window filter
#: has something to bite on.
def query_rows(now_ms: float) -> list:
    return [
        _q(1, "acme_oms", "success", now_ms - 60_000, 45.0, "Slow"),
        _q(2, "acme_oms", "failed", now_ms - 120_000, 2.0, "Slow"),
        _q(3, "acme_oms", "success", now_ms - 180_000, 1.0, "Quick"),
        _q(4, "examples", "success", now_ms - 240_000, 0.5, "Quick"),
        _q(5, "examples", "failed", now_ms - 300_000, 0.5, "Quick"),
        _q(6, "examples", "success", now_ms - 3 * 86_400_000, 90.0, "Ancient"),
    ]


RECEIVED: list[dict] = []

#: Tathya returns 401 for /api/v1/me/ regardless of token. Flip this to
#: reproduce that deployment and prove health does not depend on the endpoint.
DISABLE_ME_ENDPOINT = False


def _jwt(subject: str, ttl: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": subject, "exp": int(time.time()) + ttl}).encode()
    ).decode().rstrip("=")
    return f"{header}.{payload}.signature-not-verified-by-mock"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep test output clean
        pass

    # ------------------------------------------------------------- utilities
    def _send(self, status: int, payload, *, raw: bool = False) -> None:
        body = payload if raw else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain" if raw else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode() or "{}")

    def _bearer(self) -> str | None:
        header = self.headers.get("Authorization") or ""
        return header[7:] if header.startswith("Bearer ") else None

    def _require_auth(self) -> bool:
        token = self._bearer()
        if not token or token.count(".") != 2:
            self._send(401, {"message": "Not authorized"})
            return False
        return True

    def _require_csrf(self) -> bool:
        if self.headers.get("X-CSRFToken") != CSRF_TOKEN:
            self._send(400, {"message": "CSRF token missing or incorrect"})
            return False
        if not self.headers.get("Referer"):
            self._send(400, {"message": "Referer required"})
            return False
        return True

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    # ------------------------------------------------------------------- GET
    def do_GET(self):
        path = self._path()
        if path == "/health":
            return self._send(200, b"OK", raw=True)
        if path == "/api/v1/security/csrf_token/":
            if not self._require_auth():
                return
            self.send_response(200)
            body = json.dumps({"result": CSRF_TOKEN}).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "session=mock-session; Path=/")
            self.end_headers()
            return self.wfile.write(body)
        if not self._require_auth():
            return
        if path == "/api/v1/me/":
            if DISABLE_ME_ENDPOINT:
                return self._send(401, {"message": "Not authorized"})
            return self._send(200, {"result": {"username": VALID_USER, "first_name": "Agent",
                                               "last_name": "Test", "email": "agent@example.invalid"}})
        if path == "/api/v1/query/":
            now_ms = time.time() * 1000
            rows = query_rows(now_ms)
            raw = urllib.parse.unquote_plus(self.path.split("?", 1)[1] if "?" in self.path else "")
            since = re.search(r"col:start_time,opr:gt,value:([\d.eE+]+)", raw)
            if since:
                rows = [r for r in rows if r["start_time"] > float(since.group(1))]
            status = re.search(r"col:status,opr:eq,value:'?(\w+)'?", raw)
            if status:
                rows = [r for r in rows if r["status"] == status.group(1)]
            db_id = re.search(r"col:database,opr:rel_o_m,value:(\d+)", raw)
            if db_id:
                name = next((d["database_name"] for d in DATABASES
                             if d["id"] == int(db_id.group(1))), None)
                rows = [r for r in rows if r["database"]["database_name"] == name]
            return self._send(200, {"count": len(rows), "result": rows})
        if path == "/api/v1/report/":
            return self._send(200, {"count": len(REPORTS), "result": REPORTS})
        for prefix, rows in (("/api/v1/database/", DATABASES), ("/api/v1/dashboard/", DASHBOARDS),
                             ("/api/v1/chart/", CHARTS), ("/api/v1/dataset/", DATASETS)):
            if path == prefix:
                # The rison `q` arrives percent-encoded; decode before matching.
                raw = self.path.split("?", 1)[1] if "?" in self.path else ""
                query = urllib.parse.unquote_plus(raw)
                id_match = re.search(r"col:id,opr:eq,value:(\d+)", query)
                if id_match:
                    wanted = int(id_match.group(1))
                    rows = [r for r in rows if r.get("id") == wanted]
                return self._send(200, {"count": len(rows), "result": rows})
        match = re.match(r"^/api/v1/chart/(\d+)/data/$", path)
        if match:
            return self._send(200, {"result": [{"data": [{"stuck": 143}]}]})
        match = re.match(r"^/api/v1/(chart|dashboard|dataset)/(\d+)$", path)
        if match:
            kind, ident = match.group(1), int(match.group(2))
            table = {"chart": CHARTS, "dashboard": DASHBOARDS, "dataset": DATASETS}[kind]
            for row in table:
                if row["id"] == ident:
                    if kind == "chart":
                        # Superset's chart DETAIL response omits datasource_id and
                        # exposes the dataset only inside params. Mirrored here so
                        # the suite catches a resolver that reads the wrong field.
                        detail = {k: v for k, v in row.items() if k != "datasource_id"}
                        if row.get("datasource_id"):
                            detail["params"] = json.dumps(
                                {"datasource": f"{row['datasource_id']}__table",
                                 "viz_type": row.get("viz_type")}
                            )
                        return self._send(200, {"result": detail})
                    return self._send(200, {"result": row})
            return self._send(404, {"message": "Not found"})
        if re.match(r"^/api/v1/dashboard/[^/]+/charts$", path):
            return self._send(200, {"result": [{"slice_name": "Stuck Orders",
                                                "viz_type": "big_number_total",
                                                "form_data": {"slice_id": 10}}]})
        if re.match(r"^/api/v1/dashboard/[^/]+/datasets$", path):
            return self._send(200, {"result": DATASETS})
        if re.match(r"^/api/v1/(dashboard|chart|dataset)/export/$", path):
            return self._send(200, b"PK\x03\x04mock-zip-bytes", raw=True)
        return self._send(404, {"message": f"no mock route for GET {path}"})

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        path = self._path()
        if path == "/api/v1/security/login":
            body = self._body()
            if body.get("username") != VALID_USER or body.get("password") != VALID_PASSWORD:
                return self._send(401, {"message": "Bad credentials"})
            return self._send(200, {"access_token": _jwt(VALID_USER, ACCESS_TTL),
                                    "refresh_token": _jwt(VALID_USER, 86400)})
        if path == "/api/v1/security/refresh":
            if not self._bearer():
                return self._send(401, {"message": "Not authorized"})
            return self._send(200, {"access_token": _jwt(VALID_USER, ACCESS_TTL)})
        if not self._require_auth() or not self._require_csrf():
            return
        body = self._body()
        RECEIVED.append({"method": "POST", "path": path, "body": body})

        if path == "/api/v1/sqllab/execute/":
            # Query.client_id is String(11) in Superset; a longer value fails the
            # metadata insert with this exact opaque error. Reproduced so the
            # suite catches a regression that cost real debugging time.
            client_id = body.get("client_id")
            if client_id is not None and len(str(client_id)) > 11:
                return self._send(500, {"errors": [{"error_type": "GENERIC_DB_ENGINE_ERROR",
                                                    "message": "Create failed"}]})
            sql = body.get("sql") or ""
            # Enforce the contract the guard is supposed to guarantee.
            if WRITE_KEYWORDS.search(sql):
                return self._send(400, {"errors": [{"message": "mock refused a write statement",
                                                    "error_type": "GENERIC_DB_ENGINE_ERROR"}]})
            if "LIMIT" not in sql.upper():
                return self._send(400, {"errors": [{"message": "mock refused an unlimited query",
                                                    "error_type": "GENERIC_DB_ENGINE_ERROR"}]})
            return self._send(200, {"status": "success", "query_id": 991,
                                    "columns": [{"name": "stuck", "type": "BIGINT"}],
                                    "data": [{"stuck": 143}]})
        if path == "/api/v1/sqllab/estimate/":
            return self._send(200, {"result": [{"language": "sql", "cost": {"rows": 1200}}]})
        if path == "/api/v1/chart/":
            return self._send(201, {"id": 99, "result": body})
        if path == "/api/v1/dashboard/":
            return self._send(201, {"id": 98, "result": body})
        if path == "/api/v1/dataset/":
            return self._send(201, {"id": 97, "result": body})
        return self._send(404, {"message": f"no mock route for POST {path}"})

    # ------------------------------------------------------------------- PUT
    def do_PUT(self):
        if not self._require_auth() or not self._require_csrf():
            return
        body = self._body()
        RECEIVED.append({"method": "PUT", "path": self._path(), "body": body})
        return self._send(200, {"result": body})

    def do_DELETE(self):
        if not self._require_auth() or not self._require_csrf():
            return
        RECEIVED.append({"method": "DELETE", "path": self._path(), "body": {}})
        return self._send(200, {"message": "OK"})


def serve(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


if __name__ == "__main__":
    import sys

    srv, url = serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8088)
    print(url, flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
