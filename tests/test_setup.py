"""第一次运行的准备、设置页的检查和保存、密钥加密、称呼替换。"""
import sys
import tempfile
import unittest
from pathlib import Path

from xiaojing import secrets
from xiaojing.app import load_system_prompt
from xiaojing.config import ROOT, dump, load
from xiaojing.setup import DEFAULT_FILES, apply, ensure_defaults, missing, parse_ids, save, state

from .helpers import make_cfg


class DefaultsTest(unittest.TestCase):
    def test_puts_missing_files_and_leaves_existing_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "persona.md").write_text("我自己的人设", encoding="utf-8")
            created = ensure_defaults(root)
            self.assertEqual(created, ["config.toml", "prompts/system.md"])
            self.assertEqual((root / "persona.md").read_text(encoding="utf-8"), "我自己的人设")
            self.assertEqual(ensure_defaults(root), [])
            self.assertEqual(len(DEFAULT_FILES), 3)


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(self.tmp.name)
        self.cfg.gate.owner_qq, self.cfg.onebot.token, self.cfg.llm.api_key = "", "", ""

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_ids(self):
        self.assertEqual(parse_ids("123456, 234567，123456 abc 99"), ["123456", "234567"])

    def test_missing_and_errors(self):
        self.assertEqual(missing(self.cfg), ["主人的 QQ 号", "DeepSeek API key", "OneBot 的 HTTP token"])
        _, _, errors = apply(self.cfg, {"owner_qq": "12", "api_url": "ftp://x"})
        self.assertEqual(set(errors), {"owner_qq", "api_url", "api_key", "token"})

    def test_good_values(self):
        new, saved, errors = apply(self.cfg, {
            "owner_qq": "10001", "owner_name": "", "allow_groups": "123456 234567",
            "allow_users": "10001, 20002", "api_url": "http://127.0.0.1:3020/", "token": "t", "api_key": "k"})
        self.assertEqual(errors, {})
        self.assertEqual(new.gate.owner_name, "主人")
        self.assertEqual(new.gate.allow_users, ["10001", "20002"])  # 主人自动加上、不重复
        self.assertEqual(new.onebot.api_url, "http://127.0.0.1:3020")
        self.assertEqual(saved, {"DEEPSEEK_API_KEY": "k", "ONEBOT_TOKEN": "t", "ONEBOT_SECRET": ""})
        self.assertEqual(self.cfg.gate.owner_qq, "")  # 原配置没被改

    def test_existing_secrets_can_stay_blank(self):
        self.cfg.llm.api_key, self.cfg.onebot.token = "old-k", "old-t"
        _, saved, errors = apply(self.cfg, {"owner_qq": "10001", "api_url": "http://127.0.0.1:3020"})
        self.assertEqual(errors, {})
        self.assertFalse(any(saved.values()))

    def test_state_never_contains_secret_values(self):
        self.cfg.llm.api_key = "sk-very-secret"
        s = state(self.cfg, Path(self.tmp.name))
        self.assertTrue(s["has_key"])
        self.assertNotIn("sk-very-secret", repr(s))


class DumpTest(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            cfg.gate.owner_name = '阿"南'
            cfg.data_dir = ROOT / "data"
            p = Path(tmp) / "config.toml"
            p.write_text(dump(cfg), encoding="utf-8")
            again = load(p, env={})
            for part in ("onebot", "gate", "inbox", "rhythm", "console"):
                a, b = getattr(cfg, part), getattr(again, part)
                for k in vars(b):
                    if k not in ("token", "secret"):
                        self.assertEqual(getattr(a, k), getattr(b, k), f"{part}.{k}")
            self.assertEqual(again.llm.model, cfg.llm.model)
            self.assertEqual(again.data_dir, ROOT / "data")
            self.assertNotIn("tok", p.read_text(encoding="utf-8"))  # 密钥不写进配置


@unittest.skipUnless(sys.platform == "win32", "只有 Windows 有 DPAPI")
class SecretsTest(unittest.TestCase):
    def test_encrypted_roundtrip_and_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            secrets.save(d, {"DEEPSEEK_API_KEY": "sk-abc", "ONEBOT_TOKEN": "t1", "OTHER": "x"})
            self.assertNotIn(b"sk-abc", (d / "secrets.bin").read_bytes())
            secrets.save(d, {"ONEBOT_TOKEN": "t2", "DEEPSEEK_API_KEY": ""})  # 空的不覆盖
            self.assertEqual(secrets.load(d), {"DEEPSEEK_API_KEY": "sk-abc", "ONEBOT_TOKEN": "t2"})

    def test_save_writes_config_and_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_cfg(tmp)
            cfg.data_dir = root / "data"
            (root / "config.toml").write_text("# 旧的", encoding="utf-8")
            save(cfg, {"DEEPSEEK_API_KEY": "sk-new"}, root)
            self.assertEqual((root / "config.toml.bak").read_text(encoding="utf-8"), "# 旧的")
            self.assertIn("owner_qq", (root / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(secrets.load(cfg.data_dir)["DEEPSEEK_API_KEY"], "sk-new")


class OwnerNameTest(unittest.TestCase):
    def test_prompt_uses_configured_name(self):
        cfg = make_cfg(".")
        cfg.gate.owner_name = "阿南"
        prompt = load_system_prompt(cfg)
        self.assertNotIn("{owner_name}", prompt)
        self.assertIn("只有阿南私聊能叫醒你", prompt)
        self.assertIn(f"阿南的 QQ 号是 {cfg.gate.owner_qq}", prompt)

    def test_default_persona_has_no_personal_names(self):
        text = (ROOT / "persona.default.md").read_text(encoding="utf-8")
        self.assertIn("{owner_name}", text)
        self.assertIn("你是{owner_name}的朋友", text)  # 称呼从配置来，不写死名字


if __name__ == "__main__":
    unittest.main()
