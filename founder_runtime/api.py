"""Phase 5 — Local API server for the Founder Console.

Serves the dashboard HTML + JSON endpoints that read the durable state.

Run:
    uv run python -m founder_runtime.api
    open http://127.0.0.1:8089/dashboard/index.html
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .store import (
    Store,
    DEFAULT_DB_PATH,
    list_nodes,
    list_opportunities,
    queue_metrics,
    list_pending_approvals,
    append_audit,
)
from .founder_loop import morning_brief


class Handler(BaseHTTPRequestHandler):
    store: Store | None = None

    def log_message(self, format, *args):  # noqa: A002
        # Quiet by default; uncomment for debug:
        # super().log_message(format, *args)
        pass

    def _send_json(self, payload: dict | list) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path) -> None:
        if not path.exists():
            self.send_response(404)
            self.end_headers()
            return
        data = path.read_bytes()
        ct = "text/html" if path.suffix == ".html" else "text/plain"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/dashboard", "/dashboard/"):
            self._serve_file(Path(__file__).parent.parent / "dashboard" / "index.html")
            return
        if path.startswith("/dashboard/"):
            self._serve_file(Path(__file__).parent.parent / path.lstrip("/"))
            return
        if path == "/api/health":
            self._send_json({"ok": True})
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", "0"))
        _ = self.rfile.read(length) if length else b""
        if self.store is None:
            self._send_json({"error": "store not initialized"})
            return

        try:
            if path == "/api/today":
                focus = list_opportunities(self.store, limit=10)
                brief = morning_brief(self.store)
                approvals = list_pending_approvals(self.store)
                self._send_json({"focus": focus, "brief": brief, "pending_approvals": approvals})
            elif path == "/api/portfolio":
                opps = list_opportunities(self.store, limit=200)
                self._send_json({"opportunities": opps})
            elif path == "/api/fleet":
                nodes = list_nodes(self.store)
                self._send_json({"nodes": nodes})
            elif path == "/api/queue":
                counts = queue_metrics(self.store)
                with self.store.read() as conn:
                    rows = conn.execute(
                        "SELECT work_item_id, work_type, objective, status, priority, lease_owner "
                        "FROM work_items ORDER BY updated_at DESC LIMIT 50"
                    ).fetchall()
                recent = [dict(r) for r in rows]
                self._send_json({"counts": counts, "recent": recent})
            elif path == "/api/failures":
                # Recent failures + dead-letters with structured detail
                with self.store.read() as conn:
                    rows = conn.execute(
                        "SELECT result_id, work_item_id, worker_id, status, summary, "
                        "error_class, completed_at, retryable "
                        "FROM work_results WHERE status IN ('FAILED', 'DEAD_LETTERED') "
                        "ORDER BY completed_at DESC LIMIT 50"
                    ).fetchall()
                # Pair with work_items for context
                failures = []
                for r in rows:
                    d = dict(r)
                    wi = conn.execute(
                        "SELECT work_type, objective, priority, attempt_count, max_attempts "
                        "FROM work_items WHERE work_item_id = ?",
                        (d["work_item_id"],)
                    ).fetchone()
                    if wi is not None:
                        d["work_type"] = wi["work_type"]
                        d["objective"] = wi["objective"]
                        d["priority"] = wi["priority"]
                        d["attempt_count"] = wi["attempt_count"]
                        d["max_attempts"] = wi["max_attempts"]
                    failures.append(d)
                # Summary counters
                f_count = sum(1 for f in failures if f["status"] == "FAILED")
                dl_count = sum(1 for f in failures if f["status"] == "DEAD_LETTERED")
                by_error_class: dict[str, int] = {}
                for f in failures:
                    ec = f.get("error_class") or "(no class)"
                    by_error_class[ec] = by_error_class.get(ec, 0) + 1
                self._send_json({
                    "summary": {"failed": f_count, "dead_lettered": dl_count,
                                "by_error_class": by_error_class},
                    "failures": failures,
                })
            elif path == "/api/queue_health":
                # Per-status counts + recent recovery events + lease recovery rate
                with self.store.read() as conn:
                    counts_row = conn.execute(
                        "SELECT status, COUNT(*) AS n FROM work_items GROUP BY status"
                    ).fetchall()
                    counts = {r["status"]: r["n"] for r in counts_row}
                    # Lease recovery rate: recovered expired leases in last 24h
                    recovered_row = conn.execute(
                        "SELECT COUNT(*) AS n FROM audit_log "
                        "WHERE actor='dispatcher' AND action='tick' "
                        "AND detail LIKE '%expired_leases_recovered%' "
                        "AND created_at > datetime('now', '-1 day')"
                    ).fetchone()
                    recovered_24h = recovered_row["n"] if recovered_row else 0
                    # Stale leases right now
                    stale_row = conn.execute(
                        "SELECT COUNT(*) AS n FROM work_items "
                        "WHERE status='LEASED' AND lease_expires_at IS NOT NULL "
                        "AND lease_expires_at < datetime('now')"
                    ).fetchone()
                    stale_leases = stale_row["n"] if stale_row else 0
                self._send_json({
                    "counts": counts,
                    "expired_leases_recovered_24h": recovered_24h,
                    "stale_leases_now": stale_leases,
                })
            elif path == "/api/nodes":
                # Per-node live load (current_load + max_concurrency) + last heartbeat age
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc)
                with self.store.read() as conn:
                    rows = conn.execute(
                        "SELECT node_id, hostname, status, current_load, max_concurrency, "
                        "last_heartbeat, tailnet_address, capabilities, health_details "
                        "FROM nodes ORDER BY node_id"
                    ).fetchall()
                nodes = []
                for r in rows:
                    d = dict(r)
                    try:
                        caps = json.loads(d.pop("capabilities") or "[]")
                    except Exception:
                        caps = []
                    try:
                        hd = json.loads(d.pop("health_details") or "{}")
                    except Exception:
                        hd = {}
                    d["capabilities"] = caps
                    d["health_details"] = hd
                    hb = d.get("last_heartbeat")
                    age_seconds = None
                    if hb:
                        try:
                            hb_dt = datetime.fromisoformat(hb)
                            age_seconds = round((now - hb_dt).total_seconds(), 1)
                        except Exception:
                            age_seconds = None
                    d["heartbeat_age_seconds"] = age_seconds
                    d["load_pct"] = (round(100 * d["current_load"] / d["max_concurrency"], 1)
                                     if d["max_concurrency"] else 0)
                    nodes.append(d)
                self._send_json({"nodes": nodes})
            elif path == "/api/evidence":
                with self.store.read() as conn:
                    rows = conn.execute(
                        "SELECT work_item_id, verifier_node, verifier_model, verdict, evidence_hash, sealed_at "
                        "FROM proof_packets ORDER BY sealed_at DESC LIMIT 50"
                    ).fetchall()
                self._send_json({"proofs": [dict(r) for r in rows]})
            elif path == "/api/learning":
                with self.store.read() as conn:
                    rows = conn.execute(
                        "SELECT created_at, actor, action, target FROM audit_log ORDER BY created_at DESC LIMIT 100"
                    ).fetchall()
                self._send_json({"audit": [dict(r) for r in rows]})
            elif path.startswith("/api/opportunity/"):
                opp_id = path.split("/")[-1]
                with self.store.read() as conn:
                    opp = conn.execute("SELECT * FROM opportunities WHERE opportunity_id = ?", (opp_id,)).fetchone()
                if opp is None:
                    self._send_json({"error": "not found"})
                else:
                    self._send_json(dict(opp))
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as exc:
            self._send_json({"error": str(exc)})


def serve(host: str = "127.0.0.1", port: int = 8089, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    store = Store(db_path)
    Handler.store = store
    server = HTTPServer((host, port), Handler)
    print(f"RIG Founder Console: http://{host}:{port}/dashboard/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("shutting down")
    finally:
        store.close()


if __name__ == "__main__":
    serve()