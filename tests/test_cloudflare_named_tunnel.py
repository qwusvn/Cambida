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
