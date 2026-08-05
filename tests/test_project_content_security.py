import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_framework import KeywordRagIndex, make_file_tools


class ProjectContentSecurityTests(unittest.TestCase):
    def test_list_files_excludes_symbolic_link_to_file(self):
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            root = workspace / "project"
            root.mkdir()
            (root / "ordinary.txt").write_text("ordinary", encoding="utf-8")
            outside = workspace / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            (root / "linked-outside.txt").symlink_to(outside)
            (root / "linked-inside.txt").symlink_to(root / "ordinary.txt")
            tools = {item.name: item for item in make_file_tools(root)}

            listed = tools["list_files"].run({})

        self.assertIn("ordinary.txt", listed)
        self.assertNotIn("linked-outside.txt", listed)
        self.assertNotIn("linked-inside.txt", listed)

    def test_read_text_file_rejects_symbolic_link_to_file_inside_root(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "ordinary.txt"
            target.write_text("ordinary", encoding="utf-8")
            (root / "linked.txt").symlink_to(target)
            tools = {item.name: item for item in make_file_tools(root)}

            with self.assertRaises(ValueError):
                tools["read_text_file"].run({"path": "linked.txt"})

    def test_read_text_file_rejects_descendant_of_symbolic_link_directory(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target_dir = root / "ordinary"
            target_dir.mkdir()
            (target_dir / "secret.txt").write_text("secret", encoding="utf-8")
            (root / "linked-dir").symlink_to(target_dir, target_is_directory=True)
            tools = {item.name: item for item in make_file_tools(root)}

            with self.assertRaises(ValueError):
                tools["read_text_file"].run({"path": "linked-dir/secret.txt"})

    def test_rag_directory_index_excludes_symbolic_link_to_file(self):
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            root = workspace / "project"
            root.mkdir()
            outside = workspace / "outside.md"
            outside.write_text("symlink-only-secret", encoding="utf-8")
            (root / "linked.md").symlink_to(outside)

            index = KeywordRagIndex.from_directory(root)

        self.assertEqual(index.search("symlink-only-secret"), [])

    def test_rag_directory_index_excludes_link_to_file_inside_root(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "ordinary.md"
            target.write_text("ordinary-index-content", encoding="utf-8")
            (root / "linked.md").symlink_to(target)

            index = KeywordRagIndex.from_directory(root)

        sources = [
            result.chunk.source
            for result in index.search("ordinary-index-content", top_k=5)
        ]
        self.assertEqual(sources, ["ordinary.md"])

    def test_search_text_excludes_symbolic_link_to_file(self):
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            root = workspace / "project"
            root.mkdir()
            outside = workspace / "outside.txt"
            outside.write_text("symlink-only-secret", encoding="utf-8")
            (root / "linked.txt").symlink_to(outside)
            tools = {item.name: item for item in make_file_tools(root)}

            result = tools["search_text"].run({"query": "symlink-only-secret"})

        self.assertEqual(result, "No matches.")

    def test_list_files_excludes_symbolic_link_directory_and_descendants(self):
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            root = workspace / "project"
            root.mkdir()
            outside_dir = workspace / "outside"
            outside_dir.mkdir()
            (outside_dir / "secret.txt").write_text("secret", encoding="utf-8")
            (root / "linked-dir").symlink_to(outside_dir, target_is_directory=True)
            tools = {item.name: item for item in make_file_tools(root)}

            listed = tools["list_files"].run({})

        self.assertEqual(listed, "No matching files.")

    def test_rag_directory_index_excludes_symbolic_link_directory_descendants(self):
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            root = workspace / "project"
            root.mkdir()
            outside_dir = workspace / "outside"
            outside_dir.mkdir()
            (outside_dir / "secret.md").write_text(
                "symlink-directory-secret", encoding="utf-8"
            )
            (root / "linked-dir").symlink_to(outside_dir, target_is_directory=True)

            index = KeywordRagIndex.from_directory(root)

        self.assertEqual(index.search("symlink-directory-secret"), [])

    def test_symbolic_link_project_root_uses_its_resolved_directory(self):
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target_root = workspace / "ordinary-project"
            target_root.mkdir()
            (target_root / "guide.md").write_text(
                "ordinary-root-content", encoding="utf-8"
            )
            linked_root = workspace / "linked-project"
            linked_root.symlink_to(target_root, target_is_directory=True)

            tools = {item.name: item for item in make_file_tools(linked_root)}
            listed = tools["list_files"].run({})
            index = KeywordRagIndex.from_directory(linked_root)

        self.assertEqual(listed, "guide.md")
        self.assertEqual(index.search("ordinary-root-content")[0].chunk.source, "guide.md")

    def test_traversal_ignores_broken_links_and_symbolic_link_cycles(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "ordinary.md").write_text("ordinary-content", encoding="utf-8")
            (root / "broken.md").symlink_to(root / "missing.md")
            (root / "loop").symlink_to(root, target_is_directory=True)
            tools = {item.name: item for item in make_file_tools(root)}

            listed = tools["list_files"].run({})
            search_result = tools["search_text"].run({"query": "ordinary-content"})
            index = KeywordRagIndex.from_directory(root)

        self.assertEqual(listed, "ordinary.md")
        self.assertIn("ordinary.md:1", search_result)
        self.assertEqual(index.search("ordinary-content")[0].chunk.source, "ordinary.md")

    def test_read_text_file_rejects_absolute_path_outside_root(self):
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            root = workspace / "project"
            root.mkdir()
            outside = workspace / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            tools = {item.name: item for item in make_file_tools(root)}

            with self.assertRaises(ValueError):
                tools["read_text_file"].run({"path": str(outside)})

    def test_read_text_file_rejects_outside_link_that_targets_inside_root(self):
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            root = workspace / "project"
            root.mkdir()
            target = root / "ordinary.txt"
            target.write_text("ordinary", encoding="utf-8")
            outside_link = workspace / "outside-link.txt"
            outside_link.symlink_to(target)
            tools = {item.name: item for item in make_file_tools(root)}

            with self.assertRaisesRegex(ValueError, "^Path outside project root:"):
                tools["read_text_file"].run({"path": str(outside_link)})

    def test_read_text_file_keeps_absolute_path_inside_selected_root(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "ordinary.txt"
            target.write_text("ordinary", encoding="utf-8")
            tools = {item.name: item for item in make_file_tools(root)}

            result = tools["read_text_file"].run({"path": str(target)})

        self.assertEqual(result, "ordinary")


if __name__ == "__main__":
    unittest.main()
