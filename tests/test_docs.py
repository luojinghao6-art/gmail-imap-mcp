"""Packaging docs must name the package that actually exists on the index."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InstallCommandTests(unittest.TestCase):
    def setUp(self):
        self.files = [ROOT / "skill" / "SKILL.md", ROOT / "README.md"]
        self.texts = [(p.name, p.read_text(encoding="utf-8")) for p in self.files]

    def test_install_lines_use_the_real_package_name(self):
        for name, text in self.texts:
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("pip install gmail-imap"):
                    self.assertTrue(
                        stripped.startswith("pip install gmail-imap-mcp"),
                        f"{name}: {stripped}",
                    )
                if stripped.startswith("uvx --from gmail-imap"):
                    self.assertTrue(
                        stripped.startswith("uvx --from gmail-imap-mcp"),
                        f"{name}: {stripped}",
                    )

    def test_attachment_example_stays_inside_the_default_folder(self):
        skill = (ROOT / "skill" / "SKILL.md").read_text(encoding="utf-8")
        for line in skill.splitlines():
            if "--save-attachments" in line:
                self.assertIn("Downloads/gmail-imap", line)


class SourceHygieneTests(unittest.TestCase):
    def test_no_duplicate_top_level_definitions(self):
        """A second definition silently shadows the first - catch it in CI."""
        package = ROOT / "src" / "gmail_imap_mcp"
        files = list(package.rglob("*.py"))
        self.assertTrue(files)
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = [
                node.name for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            ]
            duplicates = sorted({name for name in names if names.count(name) > 1})
            self.assertEqual(duplicates, [], f"{path.name}: {duplicates}")


if __name__ == "__main__":
    unittest.main()
