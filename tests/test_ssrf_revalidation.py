from __future__ import annotations

import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpcore
import httpx
from fastapi import HTTPException

import mcp_server.security as security
import mcp_server.tools_http as tools_http


def _dns(ip: str):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr = (ip, 0, 0, 0) if family == socket.AF_INET6 else (ip, 0)
    return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]


class SSRFRevalidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-ssrf-")
        self.state = Path(self.temp.name) / "state"
        self.base_env = {
            "MAC_MCP_STATE_DIR": str(self.state),
            "HTTP_ALLOWLIST": "*",
            "HTTP_PRIVATE_ALLOWLIST": "",
            "HTTP_HTTPS_ONLY": "false",
            "BROWSER_ALLOWLIST": "*",
            "BROWSER_PRIVATE_ALLOWLIST": "",
            "BROWSER_HTTPS_ONLY": "false",
        }
        self.env = patch.dict("os.environ", self.base_env, clear=False)
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def settings(self):
        return security.load_settings()

    # ASSURANCE: SEC-NET-001
    def test_public_to_loopback_redirect_is_blocked_before_second_request(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:8765/health"}, request=request)

        with patch.object(security.socket, "getaddrinfo", return_value=_dns("93.184.216.34")), \
             patch.object(tools_http, "_SafeHTTPTransport", return_value=httpx.MockTransport(handler)):
            with self.assertRaises(HTTPException) as ctx:
                tools_http.http_request(self.settings(), "http://public.example/start")
        self.assertEqual(400, ctx.exception.status_code)
        self.assertEqual(["http://public.example/start"], calls)

    def test_normal_public_redirect_chain_works_and_is_reported(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if request.url.host == "public.example":
                return httpx.Response(302, headers={"Location": "https://final.example/result"}, request=request)
            return httpx.Response(200, content=b"ok-public", request=request)

        def resolver(host, *args, **kwargs):
            self.assertIn(host, {"public.example", "final.example"})
            return _dns("93.184.216.34")

        with patch.object(security.socket, "getaddrinfo", side_effect=resolver), \
             patch.object(tools_http, "_SafeHTTPTransport", return_value=httpx.MockTransport(handler)):
            result = tools_http.http_request(self.settings(), "http://public.example/start")
        self.assertEqual(200, result["status"])
        self.assertEqual("ok-public", result["text"])
        self.assertEqual(1, result["redirect_count"])
        self.assertEqual(["https://final.example"], result["redirect_chain"])
        self.assertEqual(2, len(calls))

    def test_dns_answer_with_any_private_address_is_blocked(self) -> None:
        mixed = _dns("93.184.216.34") + _dns("10.0.0.7")
        with patch.object(security.socket, "getaddrinfo", return_value=mixed):
            with self.assertRaises(HTTPException) as ctx:
                security.validate_url(self.settings(), "https://mixed.example/")
        self.assertEqual(400, ctx.exception.status_code)
        self.assertIn("local/private/non-global", str(ctx.exception.detail))

    def test_metadata_link_local_resolution_is_blocked(self) -> None:
        with patch.object(security.socket, "getaddrinfo", return_value=_dns("169.254.169.254")):
            with self.assertRaises(HTTPException):
                security.validate_url(self.settings(), "http://metadata.example/latest/meta-data/")

    # ASSURANCE: SEC-NET-001
    def test_dns_rebinding_between_validation_and_connect_is_blocked_before_socket_connect(self) -> None:
        settings = self.settings()
        with patch.object(security, "resolve_host_addresses", return_value=("93.184.216.34",)):
            security.validate_url(settings, "https://rebind.example/")
        backend = tools_http._RevalidatingSyncBackend(settings.http_private_allowlist)
        with patch.object(tools_http, "resolve_host_addresses", return_value=("127.0.0.1",)), \
             patch.object(tools_http.SyncBackend, "connect_tcp") as parent_connect:
            with self.assertRaises(httpcore.ConnectError):
                backend.connect_tcp("rebind.example", 443, timeout=1.0)
        parent_connect.assert_not_called()

    def test_connect_time_public_resolution_is_pinned_to_validated_ip(self) -> None:
        backend = tools_http._RevalidatingSyncBackend([])
        with patch.object(tools_http, "resolve_host_addresses", return_value=("93.184.216.34",)), \
             patch.object(tools_http.SyncBackend, "connect_tcp", return_value="stream") as parent_connect:
            result = backend.connect_tcp("public.example", 443, timeout=1.0)
        self.assertEqual("stream", result)
        self.assertEqual("93.184.216.34", parent_connect.call_args.args[0])
        self.assertEqual(443, parent_connect.call_args.args[1])

    def test_connect_time_falls_back_only_across_already_validated_public_ips(self) -> None:
        backend = tools_http._RevalidatingSyncBackend([])
        attempts: list[str] = []

        def connect(address, port, **kwargs):
            attempts.append(address)
            if len(attempts) == 1:
                raise httpcore.ConnectError("first public address unavailable")
            return "stream-second"

        with patch.object(tools_http, "resolve_host_addresses", return_value=("93.184.216.34", "93.184.216.35")), \
             patch.object(tools_http.SyncBackend, "connect_tcp", side_effect=connect):
            result = backend.connect_tcp("public.example", 443, timeout=1.0)
        self.assertEqual("stream-second", result)
        self.assertEqual(["93.184.216.34", "93.184.216.35"], attempts)

    def test_explicit_private_allowlist_allows_named_local_development_host(self) -> None:
        with patch.dict("os.environ", {"HTTP_PRIVATE_ALLOWLIST": "localhost"}, clear=False), \
             patch.object(security.socket, "getaddrinfo", return_value=_dns("127.0.0.1")):
            settings = security.load_settings()
            security.validate_url(settings, "http://localhost:8765/health")
        backend = tools_http._RevalidatingSyncBackend(["localhost"])
        with patch.object(tools_http, "resolve_host_addresses", return_value=("127.0.0.1",)), \
             patch.object(tools_http.SyncBackend, "connect_tcp", return_value="local-stream") as parent_connect:
            self.assertEqual("local-stream", backend.connect_tcp("localhost", 8765))
        self.assertEqual("127.0.0.1", parent_connect.call_args.args[0])

    def test_private_allowlist_wildcard_is_not_a_private_network_bypass(self) -> None:
        with patch.dict("os.environ", {"HTTP_PRIVATE_ALLOWLIST": "*"}, clear=False), \
             patch.object(security.socket, "getaddrinfo", return_value=_dns("10.0.0.2")):
            with self.assertRaises(HTTPException):
                security.validate_url(security.load_settings(), "http://internal.example/")

    def test_url_userinfo_and_non_http_schemes_are_rejected(self) -> None:
        with patch.object(security.socket, "getaddrinfo", return_value=_dns("93.184.216.34")):
            with self.assertRaises(HTTPException):
                security.validate_url(self.settings(), "http://user:pass@public.example/")
            with self.assertRaises(HTTPException):
                security.validate_url(self.settings(), "ftp://public.example/file")

    def test_safe_transport_installs_revalidating_backend_and_disables_env_proxy_path(self) -> None:
        transport = tools_http._SafeHTTPTransport(self.settings())
        try:
            self.assertIsInstance(transport._pool._network_backend, tools_http._RevalidatingSyncBackend)
        finally:
            transport.close()

    def test_browser_initial_private_resolution_is_blocked_before_navigation(self) -> None:
        import mcp_server.tools_browser as tools_browser
        with patch.object(security.socket, "getaddrinfo", return_value=_dns("127.0.0.1")), \
             patch.object(tools_browser, "_run_osascript") as run_script:
            with self.assertRaises(HTTPException) as ctx:
                tools_browser.browser_open_url(self.settings(), "Safari", "http://internal.example/")
        self.assertEqual(400, ctx.exception.status_code)
        run_script.assert_not_called()

    def test_browser_public_to_private_observed_redirect_is_closed_and_blocked(self) -> None:
        import mcp_server.tools_browser as tools_browser
        created = {
            "browser": "Google Chrome", "window_index": 1, "tab_index": 2,
            "tab_handle": "btab_test", "native_id": "7", "url": "http://localhost:8765/health",
        }
        def resolver(host, *args, **kwargs):
            return _dns("93.184.216.34") if host == "public.example" else _dns("127.0.0.1")
        with patch.object(security.socket, "getaddrinfo", side_effect=resolver), \
             patch.object(tools_browser, "_chrome_is_running", return_value=True), \
             patch.object(tools_browser, "_open_chrome_background_tab_via_extension", return_value=(created, {"chrome_tab_id": "7"})), \
             patch.object(tools_browser, "_close_unsafe_new_tab_best_effort") as close_unsafe:
            with self.assertRaises(HTTPException) as ctx:
                tools_browser.browser_open_url(self.settings(), "Google Chrome", "https://public.example/start")
        self.assertEqual(403, ctx.exception.status_code)
        self.assertEqual("browser_redirect_blocked", ctx.exception.detail["error"])
        close_unsafe.assert_called_once()

    def test_browser_normal_public_observed_redirect_succeeds(self) -> None:
        import mcp_server.tools_browser as tools_browser
        created = {
            "browser": "Google Chrome", "window_index": 1, "tab_index": 2,
            "tab_handle": "btab_test", "native_id": "7", "url": "https://final.example/result",
        }
        with patch.object(security.socket, "getaddrinfo", return_value=_dns("93.184.216.34")), \
             patch.object(tools_browser, "_chrome_is_running", return_value=True), \
             patch.object(tools_browser, "_open_chrome_background_tab_via_extension", return_value=(created, {"chrome_tab_id": "7"})), \
             patch.object(tools_browser.browser_tabs, "claim_created_tab", return_value={"generation": 1}), \
             patch.object(tools_browser, "_claim_tab_visual", return_value=False):
            result = tools_browser.browser_open_url(self.settings(), "Google Chrome", "https://public.example/start")
        self.assertEqual("https://final.example/result", result["url"])
        self.assertEqual("https://public.example/start", result["requested_url"])

    # ASSURANCE: SEC-NET-001
    def test_browser_rebinding_same_hostname_is_caught_on_observed_revalidation(self) -> None:
        import mcp_server.tools_browser as tools_browser
        created = {
            "browser": "Google Chrome", "window_index": 1, "tab_index": 2,
            "tab_handle": "btab_test", "native_id": "7", "url": "https://rebind.example/after",
        }
        answers = [_dns("93.184.216.34"), _dns("127.0.0.1")]
        with patch.object(security.socket, "getaddrinfo", side_effect=answers), \
             patch.object(tools_browser, "_chrome_is_running", return_value=True), \
             patch.object(tools_browser, "_open_chrome_background_tab_via_extension", return_value=(created, {"chrome_tab_id": "7"})), \
             patch.object(tools_browser, "_close_unsafe_new_tab_best_effort") as close_unsafe:
            with self.assertRaises(HTTPException) as ctx:
                tools_browser.browser_open_url(self.settings(), "Google Chrome", "https://rebind.example/start")
        self.assertEqual(403, ctx.exception.status_code)
        close_unsafe.assert_called_once()

    def test_browser_private_allowlist_is_explicit_and_separate_from_public_allowlist(self) -> None:
        import mcp_server.tools_browser as tools_browser
        with patch.dict("os.environ", {"BROWSER_PRIVATE_ALLOWLIST": "localhost"}, clear=False), \
             patch.object(security.socket, "getaddrinfo", return_value=_dns("127.0.0.1")):
            tools_browser.validate_url(security.load_settings(), "http://localhost:8765/")

    def test_existing_tab_unsafe_observation_requests_restore(self) -> None:
        import mcp_server.tools_browser as tools_browser
        row = {"window_index": 1, "tab_index": 1, "url": "http://localhost:8765/"}
        with patch.object(security.socket, "getaddrinfo", return_value=_dns("127.0.0.1")), \
             patch.object(tools_browser, "_restore_previous_tab_url_best_effort") as restore:
            with self.assertRaises(HTTPException):
                tools_browser._validate_observed_navigation(
                    self.settings(), "Safari", "https://public.example/", row,
                    new_tab=False, previous_url="https://previous.example/",
                )
        restore.assert_called_once()


if __name__ == "__main__":
    unittest.main()
