import json
import tempfile
from pathlib import Path
import unittest

from src.utils.fingerprint import (
    BrowserFingerprint,
    generate_fingerprint_for_account,
    get_or_create_fingerprint,
    fingerprint_file_path,
)


class TestFingerprint(unittest.TestCase):

    def test_generate_fingerprint_consistency(self):
        # Одинаковый account_id даёт согласованный отпечаток
        fp1 = generate_fingerprint_for_account("acc1")
        fp2 = generate_fingerprint_for_account("acc1")
        self.assertEqual(fp1.platform, fp2.platform)
        self.assertEqual(fp1.webgl_vendor, fp2.webgl_vendor)
        self.assertEqual(fp1.webgl_renderer, fp2.webgl_renderer)
        self.assertEqual(fp1.screen_width, fp2.screen_width)

    def test_fingerprint_persists_to_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            profile_dir = Path(tmp_dir)
            
            # Первый вызов: создаёт и сохраняет
            fp1 = get_or_create_fingerprint(profile_dir, account_id="manager1")
            file_path = fingerprint_file_path(profile_dir)
            self.assertTrue(file_path.exists())

            # Читаем сырой JSON
            data = json.loads(file_path.read_text(encoding="utf-8"))
            self.assertEqual(data["account_id"], "manager1")
            self.assertEqual(data["platform"], fp1.platform)

            # Второй вызов: должен прочитать тот же самый файл без изменений
            fp2 = get_or_create_fingerprint(profile_dir, account_id="manager1")
            self.assertEqual(fp1.created_at, fp2.created_at)
            self.assertEqual(fp1.user_agent, fp2.user_agent)
            self.assertEqual(fp1.webgl_renderer, fp2.webgl_renderer)

    def test_stealth_script_generation(self):
        fp = generate_fingerprint_for_account("main")
        script = fp.generate_stealth_script()
        
        # Проверяем ключевые элементы маскировки
        self.assertIn("'webdriver'", script)
        self.assertIn(fp.platform, script)
        self.assertIn(fp.webgl_renderer, script)
        self.assertIn("UNMASKED_RENDERER_WEBGL", script)
        self.assertIn("devicePixelRatio", script)
        self.assertIn("PDF Viewer", script)
        self.assertIn("window.chrome", script)


if __name__ == "__main__":
    unittest.main()
