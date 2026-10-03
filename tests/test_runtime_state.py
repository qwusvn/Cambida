import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("runtime_state_tested", ROOT / "runtime_state.py")
STATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STATE)


class RuntimeStateTests(unittest.TestCase):
    def _seed_shop(self, base: Path):
        config = {
            "server_port": 8000,
            "cloudflare_subdomain": "shop.example.com",
            "cloudflare_bound_subdomain": "shop.example.com",
            "cloudflare_bound_port": 8000,
            "public_base_url": "https://shop.example.com",
            "site": {"name": "Existing shop"},
        }
        (base / "config.json").write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8"
        )
        (base / "tunnel_token.txt").write_text("tunnel-token\n", encoding="utf-8")
        (base / "controlhub_machine_id.txt").write_text("machine-1\n", encoding="utf-8")
        (base / "controlhub_client_secret.txt").write_text("client-secret\n", encoding="utf-8")
        (base / "controlhub_bootstrap.json").write_text(
            '{"site_id":"legacy-site"}', encoding="utf-8"
        )
        return config

    def test_missing_runtime_files_restore_port_token_and_identity_after_manual_copy(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as vd:
            base = Path(td)
            vault = Path(vd)
            expected = self._seed_shop(base)
            STATE.snapshot_runtime_state(base, vault_path=vault)

            for name in (
                "config.json",
                "tunnel_token.txt",
                "controlhub_machine_id.txt",
                "controlhub_client_secret.txt",
                "controlhub_bootstrap.json",
            ):
                (base / name).unlink()

            # Simulate full release files being copied into the same install tree.
            (base / "Cambida.exe").write_bytes(b"new-release")
            restored = STATE.restore_runtime_state(base, vault_path=vault)

            self.assertIn("config.json", restored)
            self.assertIn("tunnel_token.txt", restored)
            self.assertEqual(
                json.loads((base / "config.json").read_text(encoding="utf-8"))["server_port"],
                8000,
            )
            self.assertEqual(
                json.loads((base / "config.json").read_text(encoding="utf-8"))["site"],
                expected["site"],
            )
            self.assertEqual((base / "tunnel_token.txt").read_text(encoding="utf-8").strip(), "tunnel-token")
            self.assertEqual((base / "controlhub_machine_id.txt").read_text(encoding="utf-8").strip(), "machine-1")
            self.assertEqual((base / "controlhub_client_secret.txt").read_text(encoding="utf-8").strip(), "client-secret")

    def test_restore_is_idempotent_and_never_overwrites_existing_config(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as vd:
            base = Path(td)
            vault = Path(vd)
            self._seed_shop(base)
            STATE.snapshot_runtime_state(base, vault_path=vault)
            (base / "config.json").unlink()

            STATE.restore_runtime_state(base, vault_path=vault)
            first = (base / "config.json").read_bytes()
            first_token = (base / "tunnel_token.txt").read_bytes()

            # Existing runtime config wins, even if the vault contains a prior copy.
            current = json.loads(first.decode("utf-8"))
            current["server_port"] = 8123
            (base / "config.json").write_text(json.dumps(current), encoding="utf-8")
            second_restore = STATE.restore_runtime_state(base, vault_path=vault)

            self.assertEqual(second_restore, [])
            self.assertEqual(
                json.loads((base / "config.json").read_text(encoding="utf-8"))["server_port"],
                8123,
            )
            self.assertEqual((base / "tunnel_token.txt").read_bytes(), first_token)

    def test_old_token_is_not_restored_after_subdomain_change(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as vd:
            base = Path(td)
            vault = Path(vd)
            self._seed_shop(base)
            STATE.snapshot_runtime_state(base, vault_path=vault)
            (base / "tunnel_token.txt").unlink()
            cfg = json.loads((base / "config.json").read_text(encoding="utf-8"))
            cfg["cloudflare_subdomain"] = "other.example.com"
            (base / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

            STATE.restore_runtime_state(base, vault_path=vault)
            self.assertFalse((base / "tunnel_token.txt").exists())

    def test_fresh_install_has_no_vault_state_and_release_seed_defaults_to_8000(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as vd:
            base = Path(td)
            self.assertEqual(
                STATE.restore_runtime_state(base, vault_path=Path(vd)),
                [],
            )
            seed = json.loads((ROOT / "config.release.json").read_text(encoding="utf-8"))
            self.assertEqual(seed["server_port"], 8000)


if __name__ == "__main__":
    unittest.main()
