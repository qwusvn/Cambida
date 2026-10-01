import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SETUP = load_module("cambida_setup_cloudflare", ROOT / "scripts" / "setup_cloudflare.py")
UPDATER = load_module("cambida_updater", ROOT / "scripts" / "updater.py")
BUILDER = load_module("cambida_build_camhl_release", ROOT / "scripts" / "build_camhl_release.py")


class NamedTunnelUpdateTests(unittest.TestCase):
    def test_fixed_public_url_normalizes_hostname(self):
        self.assertEqual(
            SETUP.get_fixed_public_url({"public_base_url": "cam1.example.com/"}),
            "https://cam1.example.com",
        )

    def test_fixed_public_url_rejects_trycloudflare(self):
        self.assertEqual(
            SETUP.get_fixed_public_url(
                {"public_base_url": "https://random.trycloudflare.com"}
            ),
            "",
        )

    def test_token_file_is_read_separately_from_config(self):
        with tempfile.TemporaryDirectory() as td:
            token_file = Path(td) / "tunnel_token.txt"
            token_file.write_text("shop-token\n", encoding="utf-8")
            with patch.object(SETUP, "TOKEN_FILE", token_file):
                self.assertEqual(SETUP.read_tunnel_token({}), "shop-token")

    def test_updater_installs_token_once_and_preserves_config(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            Path(src, "config.json").write_text('{"new": true}', encoding="utf-8")
            Path(src, "tunnel_token.txt").write_text("new-token", encoding="utf-8")
            Path(src, "app.bin").write_text("new-app", encoding="utf-8")

            Path(dst, "config.json").write_text('{"keep": true}', encoding="utf-8")

            UPDATER.update_files(src, dst)

            self.assertEqual(Path(dst, "config.json").read_text(encoding="utf-8"), '{"keep": true}')
            self.assertEqual(Path(dst, "tunnel_token.txt").read_text(encoding="utf-8"), "new-token")
            self.assertEqual(Path(dst, "app.bin").read_text(encoding="utf-8"), "new-app")

    def test_updater_never_overwrites_existing_shop_token(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            Path(src, "tunnel_token.txt").write_text("incoming-token", encoding="utf-8")
            Path(dst, "tunnel_token.txt").write_text("existing-shop-token", encoding="utf-8")

            UPDATER.update_files(src, dst)

            self.assertEqual(
                Path(dst, "tunnel_token.txt").read_text(encoding="utf-8"),
                "existing-shop-token",
            )

    def test_private_build_includes_token_only_when_explicitly_requested(self):
        with tempfile.TemporaryDirectory() as td:
            token_file = Path(td) / "shop-token.txt"
            token_file.write_text("private-shop-token", encoding="utf-8")
            with patch("sys.argv", ["build_camhl_release.py"]):
                self.assertIsNone(BUILDER.get_tunnel_token_seed())
            with patch(
                "sys.argv",
                ["build_camhl_release.py", "--tunnel-token-file", str(token_file)],
            ):
                self.assertEqual(BUILDER.get_tunnel_token_seed(), token_file.resolve())

    def test_zone_discovery_uses_longest_matching_zone(self):
        zones = [
            {"id": "zone-root", "name": "example.com", "account": {"id": "acct"}},
            {"id": "zone-sub", "name": "shop.example.com", "account": {"id": "acct"}},
        ]
        with patch.object(SETUP, "_cloudflare_api_request", return_value=zones):
            zone = SETUP.discover_cloudflare_zone("admin-token", "cam.shop.example.com")
        self.assertEqual(zone["id"], "zone-sub")

    def test_provision_builds_tunnel_ingress_dns_and_fetches_token(self):
        calls = []

        def fake_api(method, path, api_token, payload=None, query=None, timeout=15.0):
            calls.append((method, path, payload, query))
            if path == "/zones":
                return [{"id": "zone1", "name": "example.com", "account": {"id": "acct1"}}]
            if path == "/accounts/acct1/cfd_tunnel" and method == "GET":
                return []
            if path == "/accounts/acct1/cfd_tunnel" and method == "POST":
                return {"id": "tunnel1", "name": "cambida-cam-example-com"}
            if path.endswith("/configurations"):
                return {"ok": True}
            if path == "/zones/zone1/dns_records" and method == "GET":
                return []
            if path == "/zones/zone1/dns_records" and method == "POST":
                return {"id": "dns1"}
            if path.endswith("/token"):
                return "shop-tunnel-token"
            raise AssertionError((method, path, payload, query))

        with patch.object(SETUP, "_cloudflare_api_request", side_effect=fake_api):
            result = SETUP.provision_cloudflare_named_tunnel(
                "admin-token",
                {"public_base_url": "https://cam.example.com"},
                8004,
            )

        self.assertEqual(result["tunnel_token"], "shop-tunnel-token")
        config_call = next(c for c in calls if c[1].endswith("/configurations"))
        ingress = config_call[2]["config"]["ingress"]
        self.assertEqual(ingress[0]["hostname"], "cam.example.com")
        self.assertEqual(ingress[0]["service"], "http://127.0.0.1:8004")
        dns_create = next(c for c in calls if c[0] == "POST" and c[1] == "/zones/zone1/dns_records")
        self.assertEqual(dns_create[2]["content"], "tunnel1.cfargotunnel.com")
        self.assertTrue(dns_create[2]["proxied"])

    def test_first_setup_auto_generates_tunnel_token_file(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            token_path = Path(td) / "tunnel_token.txt"
            cfg_path.write_text(
                '{"server_port": 8004, "public_base_url": "https://cam.example.com"}',
                encoding="utf-8",
            )
            provisioned = {
                "hostname": "cam.example.com",
                "zone_id": "zone1",
                "account_id": "acct1",
                "tunnel_id": "tunnel1",
                "tunnel_name": "cambida-cam-example-com",
                "tunnel_token": "generated-shop-token",
            }
            with patch.object(SETUP, "CONFIG_FILE", cfg_path), \
                 patch.object(SETUP, "TOKEN_FILE", token_path), \
                 patch.object(SETUP, "detect_server_port", return_value=8004), \
                 patch.object(SETUP, "ping_port", return_value=False), \
                 patch.object(SETUP, "provision_cloudflare_named_tunnel", return_value=provisioned) as provision, \
                 patch.object(SETUP, "run_named_tunnel", return_value=True), \
                 patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "one-time-admin-token"}):
                self.assertTrue(SETUP.action_setup(non_interactive=True))

            self.assertEqual(token_path.read_text(encoding="utf-8").strip(), "generated-shop-token")
            saved = cfg_path.read_text(encoding="utf-8")
            self.assertNotIn("one-time-admin-token", saved)
            provision.assert_called_once()

    def test_updater_installs_controlhub_bootstrap_once(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            Path(src, "controlhub_bootstrap.json").write_text('{"site_id":"new"}', encoding="utf-8")
            UPDATER.update_files(src, dst)
            self.assertEqual(
                Path(dst, "controlhub_bootstrap.json").read_text(encoding="utf-8"),
                '{"site_id":"new"}',
            )
            Path(src, "controlhub_bootstrap.json").write_text('{"site_id":"overwrite"}', encoding="utf-8")
            UPDATER.update_files(src, dst)
            self.assertEqual(
                Path(dst, "controlhub_bootstrap.json").read_text(encoding="utf-8"),
                '{"site_id":"new"}',
            )

    def test_runtime_start_function_has_no_quick_tunnel_fallback(self):
        source = (ROOT / "1.py").read_text(encoding="utf-8")
        start = source.index("def start_cloudflared_tunnel():")
        end = source.index("def stop_cloudflared_tunnel():", start)
        block = source[start:end]
        self.assertIn('"tunnel", "run", "--token"', block)
        self.assertNotIn('"--url"', block)
        self.assertNotIn("trycloudflare.com", block)


if __name__ == "__main__":
    unittest.main()
