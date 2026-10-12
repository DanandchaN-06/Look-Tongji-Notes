from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from console.upgrade import backup_sqlite_database, install_program_files, restore_program_files


class ProgramUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.backups = self.root / "backups"
        self.source.mkdir()
        self.target.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_failed_upgrade_rolls_back_every_changed_program_file(self):
        (self.source / "console").mkdir()
        (self.target / "console").mkdir()
        (self.source / "console" / "one.py").write_text("new one", encoding="utf-8")
        (self.source / "console" / "two.py").write_text("new two", encoding="utf-8")
        (self.target / "console" / "one.py").write_text("old one", encoding="utf-8")
        (self.target / "console" / "two.py").write_text("old two", encoding="utf-8")
        real_replace = __import__("os").replace
        failed = False

        def replace_then_fail_once(source, destination):
            nonlocal failed
            if Path(destination) == self.target / "console" / "two.py" and not failed:
                failed = True
                raise OSError("synthetic install interruption")
            return real_replace(source, destination)

        with patch("console.upgrade.os.replace", side_effect=replace_then_fail_once):
            with self.assertRaisesRegex(OSError, "synthetic install interruption"):
                install_program_files(
                    self.source, self.target,
                    ["console/one.py", "console/two.py"],
                    self.backups,
                )

        self.assertEqual((self.target / "console" / "one.py").read_text(encoding="utf-8"), "old one")
        self.assertEqual((self.target / "console" / "two.py").read_text(encoding="utf-8"), "old two")
        self.assertTrue(list(self.backups.iterdir()))

    def test_upgrade_rejects_user_data_and_path_traversal(self):
        (self.source / "console").mkdir()
        (self.source / "console" / "safe.py").write_text("new", encoding="utf-8")
        for path in ("../outside.txt", "state/console-v2.sqlite3", ".env", "runtime/python.exe", "courses/raw.txt"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    install_program_files(self.source, self.target, [path], self.backups)

    def test_upgrade_keeps_backup_and_updates_only_requested_files(self):
        (self.source / "console").mkdir()
        (self.target / "console").mkdir()
        (self.source / "console" / "start.py").write_text("new launcher", encoding="utf-8")
        (self.target / "console" / "start.py").write_text("old launcher", encoding="utf-8")
        (self.target / ".env").write_text("SECRET=unchanged", encoding="utf-8")
        backup = install_program_files(self.source, self.target, ["console/start.py"], self.backups)
        self.assertEqual((self.target / "console" / "start.py").read_text(encoding="utf-8"), "new launcher")
        self.assertEqual((backup / "console" / "start.py").read_text(encoding="utf-8"), "old launcher")
        self.assertEqual((self.target / ".env").read_text(encoding="utf-8"), "SECRET=unchanged")
        restore_program_files(backup, self.target)
        self.assertEqual((self.target / "console" / "start.py").read_text(encoding="utf-8"), "old launcher")

    def test_sqlite_schema_backup_passes_integrity_check(self):
        import sqlite3

        source = self.root / "console-v2.sqlite3"
        connection = sqlite3.connect(source)
        connection.execute("create table sample(value text)")
        connection.execute("insert into sample values ('preserved')")
        connection.commit()
        connection.close()
        destination = self.backups / "console-v2.sqlite3"
        self.assertEqual(backup_sqlite_database(source, destination), destination)
        backup = sqlite3.connect(destination)
        try:
            self.assertEqual(backup.execute("select value from sample").fetchone()[0], "preserved")
            self.assertEqual(backup.execute("pragma integrity_check").fetchone()[0], "ok")
        finally:
            backup.close()


if __name__ == "__main__":
    unittest.main()
