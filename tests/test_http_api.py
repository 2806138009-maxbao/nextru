"""Local HTTP interface smoke test (stdlib server, loopback only)."""

from __future__ import annotations

import json
import http.client
import threading
import time
import unittest
import urllib.error
import urllib.request

from helpers import NexusTest
from nexus.http_api import build_server


class HttpApiTest(NexusTest):
    def setUp(self) -> None:
        super().setUp()
        self.server = build_server(self.db_path, host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def _open(self, req, timeout=10):
        last = None
        for _ in range(4):
            try:
                return urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError:
                raise
            except (urllib.error.URLError, ConnectionError,
                    http.client.RemoteDisconnected) as exc:
                last = exc
                time.sleep(0.15)
        raise last

    def _get(self, path: str):
        with self._open(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def _post(self, payload: dict):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/query",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self._open(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def test_health(self):
        status, body = self._get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertIn("get_current_state", body["operations"])

    def test_query_contract_shape(self):
        status, body = self._post({
            "operation": "get_current_state",
            "params": {"object_id": "project/DLC-01", "aspect": "decision",
                       "scope": "project:DLC-01"},
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["execution"]["status"], "OK")
        self.assertEqual(body["state"]["value"]["decision_id"], "D1")
        self.assertEqual(body["authority"]["owner"], "user:owner")
        self.assertIn("retrieval_path", body["execution"])
        self.assertIn("evidence", body)

    def test_unknown_operation_is_domain_error(self):
        status, body = self._post({"operation": "drop_all_tables", "params": {}})
        self.assertEqual(status, 200)
        self.assertEqual(body["execution"]["status"], "INVALID_SCOPE")
        self.assertIn("known_operations", body["error"]["detail"])

    def test_malformed_json_is_transport_error(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/query", data=b"{not json",
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._open(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)


    def _post_to(self, path: str, payload: dict):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self._open(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def test_read_only_interface_rejects_state_changing_operations(self):
        status, body = self._post({
            "operation": "evaluate_event",
            "params": {"event_id": "ev-official-new", "scope": "project:DLC-01"},
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["execution"]["status"], "AUTHORITY_DENIED")
        decision = self.core.get_current_state(
            "project/DLC-01", "decision", "project:DLC-01")
        self.assertEqual(decision["state"]["value"]["decision_id"], "D1")
        event = self.store.get_event("ev-official-new")
        self.assertIsNone(event["processed_at"])
        self.record(
            "T4-H1", "默认 HTTP 查询接口拒绝 evaluate_event",
            {"path": "/query", "operation": "evaluate_event"},
            {"status": "AUTHORITY_DENIED", "decision": "D1", "event_processed": False},
            {"body_status": body["execution"]["status"],
             "event_processed": event["processed_at"]},
        )

    def test_test_interface_is_disabled_by_default(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post_to("/test/query", {
                "operation": "evaluate_event",
                "params": {"event_id": "ev-official-new", "scope": "project:DLC-01"},
            })
        self.assertEqual(ctx.exception.code, 403)

    def test_test_interface_rejects_client_supplied_caller(self):
        server = build_server(self.db_path, host="127.0.0.1", port=0,
                              allow_test_writes=True)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/test/query",
                data=json.dumps({
                    "operation": "evaluate_event",
                    "params": {"event_id": "ev-official-new",
                               "scope": "project:DLC-01",
                               "caller": "user:owner"},
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with self._open(req, timeout=10) as r:
                body = json.loads(r.read().decode("utf-8"))
            self.assertEqual(body["execution"]["status"], "AUTHORITY_DENIED")
            self.assertEqual(
                self.core.get_current_state(
                    "project/DLC-01", "decision", "project:DLC-01"
                )["state"]["value"]["decision_id"], "D1")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_test_interface_uses_server_principal_and_cannot_author_user_decision(self):
        server = build_server(self.db_path, host="127.0.0.1", port=0,
                              allow_test_writes=True)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def post(payload):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/test/query",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST")
                with self._open(req, timeout=10) as r:
                    return json.loads(r.read().decode("utf-8"))

            official = post({
                "operation": "evaluate_event",
                "params": {"event_id": "ev-official-new", "scope": "project:DLC-01"},
            })
            self.assertEqual(official["execution"]["status"], "OK")
            self.assertFalse(official["result"]["outcome"]["decision_changed"])

            user_decision = post({
                "operation": "evaluate_event",
                "params": {"event_id": "ev-user-decision", "scope": "project:DLC-01"},
            })
            self.assertEqual(user_decision["execution"]["status"], "AUTHORITY_DENIED")
            decision = self.core.get_current_state(
                "project/DLC-01", "decision", "project:DLC-01")
            self.assertEqual(decision["state"]["value"]["decision_id"], "D1")
            self.record(
                "T4-H2", "隔离测试接口注入服务端 principal，客户端不能冒充用户决定",
                {"official": "ev-official-new", "user_event": "ev-user-decision"},
                {"official": "OK/no decision change", "user": "AUTHORITY_DENIED"},
                {"official": official["execution"]["status"],
                 "user": user_decision["execution"]["status"],
                 "decision": decision["state"]["value"]["decision_id"]},
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_test_interface_refuses_non_loopback_bind(self):
        with self.assertRaises(ValueError):
            build_server(self.db_path, host="0.0.0.0", port=0,
                         allow_test_writes=True)


if __name__ == "__main__":
    unittest.main()
