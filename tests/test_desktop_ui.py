"""Render native widgets with inert callbacks: never start a service or log in."""
from types import SimpleNamespace
import tkinter as tk
import unittest

from desktop.ui import LauncherView, COLORS


class DesktopViewTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.owner = SimpleNamespace(root=self.root, **{name: lambda: None for name in (
            "start", "open_panel", "open_guide", "open_data", "diagnostics", "hide", "close")})
        self.view = LauncherView(self.owner, "0.1.0")
        self.root.update_idletasks()

    def tearDown(self):
        self.root.destroy()
        # Tcl objects must be collected on their creator thread, before threaded tests.
        self.view = self.owner = self.root = None
        import gc
        gc.collect()

    def test_web_theme_and_primary_action(self):
        self.assertEqual(self.root.cget("background"), COLORS["bg"])
        self.assertEqual(self.owner.open_button.cget("style"), "Primary.TButton")
        self.assertEqual(str(self.owner.open_button.cget("state")), "disabled")

    def test_ready_hides_progress_and_keeps_address(self):
        self.view.phase("starting")
        self.assertEqual(self.view.progress_row.winfo_manager(), "grid")
        self.view.phase("ready", "http://127.0.0.1:8123")
        self.assertEqual(self.view.progress_row.winfo_manager(), "")
        self.assertEqual(self.view.address.get(), "http://127.0.0.1:8123")
        self.assertEqual(self.view.badge.cget("style"), "Ready.TLabel")

    def test_failure_and_shutdown_are_distinct(self):
        self.view.phase("error")
        self.assertEqual(self.view.badge.cget("style"), "Error.TLabel")
        self.assertEqual(self.view.progress_row.winfo_manager(), "")
        self.view.phase("stopping")
        self.assertEqual(self.view.progress_row.winfo_manager(), "grid")

    def test_requested_layout_fits_default_size(self):
        page = self.root.winfo_children()[0]
        self.assertLessEqual(page.winfo_reqwidth(), 840)
        self.assertLessEqual(page.winfo_reqheight(), 670)


if __name__ == "__main__":
    unittest.main()
