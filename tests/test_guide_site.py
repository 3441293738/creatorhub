"""Dependency-free checks for public docs and the Pages integration.

Run with unittest so documentation CI does not load application dependencies.
"""
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import unquote, urlsplit

from preview.build_preview import ROOT, GUIDE, GUIDE_IMAGES, build, build_guide


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.links = []
        self.images = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if "href" in attrs:
            self.links.append(attrs["href"])
        if tag == "img":
            self.images.append(attrs)
            self.links.append(attrs["src"])


class GuideSiteTests(unittest.TestCase):
    def parse(self, path):
        parser = Links()
        parser.feed(path.read_text(encoding="utf-8"))
        return parser

    def test_unique_anchors_and_required_chapters(self):
        page = self.parse(GUIDE / "index.html")
        self.assertEqual(len(page.ids), len(set(page.ids)))
        required = {"welcome", "install", "accounts", "first-task", "monitor",
                    "collection", "comments", "download", "publish", "my-content",
                    "autocomment", "notifications", "queue", "settings", "backup",
                    "faq", "help"}
        self.assertTrue(required.issubset(page.ids))
        for section in required:
            self.assertIn(f"#{section}", page.links)
        for link in page.links:
            if link.startswith("#"):
                self.assertIn(unquote(link[1:]), page.ids)

    def test_images_have_accessible_labels_and_match_allowlist(self):
        page = self.parse(GUIDE / "index.html")
        self.assertTrue(page.images)
        self.assertEqual({Path(img["src"]).name for img in page.images}, set(GUIDE_IMAGES))
        for img in page.images:
            self.assertTrue(img.get("alt", "").strip())
            self.assertEqual(img.get("loading"), "lazy")

    def test_guide_output_is_allowlisted(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "guide"
            build_guide(target)
            actual = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
            expected = {"index.html", "guide.css"} | {f"images/{name}" for name in GUIDE_IMAGES}
            expected |= {f"{platform}/index.html" for platform in ("douyin", "xhs", "kuaishou", "shipinhao")}
            self.assertEqual(actual, expected)

    def test_full_preview_preserves_demo_and_resolves_document_links(self):
        # build() requires a destination inside ROOT. Use a fresh child of an
        # ignored generated directory, never an existing source directory.
        output_root = ROOT / "_site"
        output_root.mkdir(exist_ok=True)
        with TemporaryDirectory(prefix="guide-test-", dir=output_root) as tmp:
            target = Path(tmp) / "site"
            build(target)
            self.assertTrue((target / "demo-api.js").is_file())
            self.assertTrue((target / "community" / "index.html").is_file())
            self.assertTrue((target / ".nojekyll").is_file())
            page = self.parse(target / "guide" / "index.html")
            for link in page.links:
                parsed = urlsplit(link)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                self.assertFalse(parsed.path.startswith("/"), link)
                resolved = (target / "guide" / unquote(parsed.path)).resolve()
                self.assertTrue(resolved.is_relative_to(target), link)
                if resolved.is_dir():
                    resolved = resolved / "index.html"
                self.assertTrue(resolved.is_file(), link)
            for private in ("data", "docs", "profiles", "logs", "config.yaml"):
                self.assertFalse((target / private).exists())

    def test_all_platform_page_links_and_anchors_resolve(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp)
            build_guide(target)
            for slug in ("douyin", "xhs", "kuaishou", "shipinhao"):
                page = target / slug / "index.html"
                parsed_page = self.parse(page)
                self.assertEqual(len(parsed_page.ids), len(set(parsed_page.ids)))
                for link in parsed_page.links:
                    parsed = urlsplit(link)
                    if parsed.scheme or parsed.netloc:
                        continue
                    resolved = (page.parent / unquote(parsed.path)).resolve() if parsed.path else page
                    if resolved.is_dir():
                        resolved /= "index.html"
                    self.assertTrue(resolved.is_relative_to(target), link)
                    self.assertTrue(resolved.is_file(), link)
                    if parsed.fragment:
                        self.assertIn(unquote(parsed.fragment), self.parse(resolved).ids)


if __name__ == "__main__":
    unittest.main()
