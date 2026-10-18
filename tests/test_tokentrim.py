import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tokentrim import cli, filters, outline  # noqa: E402

HOOK = [sys.executable, os.path.join(ROOT, "tokentrim_hook.py")]


def run_hook(event, payload, home):
    env = dict(os.environ, HOME=home)
    r = subprocess.run(HOOK + [event], input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=30)
    return json.loads(r.stdout) if r.stdout.strip() else None


class Filters(unittest.TestCase):
    def test_short_output_untouched(self):
        self.assertEqual(filters.shrink_command_output("ok\n", 6000), "ok\n")

    def test_long_output_keeps_head_and_tail_and_spools(self):
        text = "\n".join("row %d value %d" % (i, i * 7) for i in range(5000))
        small = filters.shrink_command_output(text, 6000)
        self.assertLess(len(small), 6500)
        self.assertIn("row 0 value 0", small)
        self.assertIn("row 4999", small)
        path = small.split("Full output: ")[1].split(" ")[0]
        with open(path) as fh:
            self.assertEqual(fh.read(), text)  # nothing is lost

    def test_data_rows_are_not_collapsed(self):
        text = "\n".join("AAPL,%d" % i for i in range(10))
        self.assertEqual(filters.collapse_repeats(text), text)

    def test_identical_lines_collapse(self):
        text = "\n".join(["warn: x"] * 50)
        self.assertIn("48 identical lines", filters.collapse_repeats(text))

    def test_progress_bars_and_ansi(self):
        self.assertEqual(filters.strip_noise("\x1b[32m10%\r50%\r100%\x1b[0m"), "100%")

    def test_json_minify_keeps_list_items(self):
        obj = {"items": [{"id": i, "note": None, "tags": []} for i in range(30)], "meta": {"x": ""}}
        small = json.loads(filters.shrink_json_text(json.dumps(obj, indent=2)))
        self.assertEqual(len(small["items"]), 30)
        self.assertNotIn("note", small["items"][0])

    def test_non_json_passthrough(self):
        self.assertEqual(filters.shrink_json_text("hello {"), "hello {")


class Buckets(unittest.TestCase):
    def test_secrets_never_become_labels(self):
        from tokentrim.audit import _bucket
        self.assertEqual(_bucket("Bash", {"command": 'KEY="abc123==" curl -s x'}), "Bash:curl")
        self.assertEqual(_bucket("Bash", {"command": "cd /tmp && grep x y"}), "Bash:grep")
        self.assertEqual(_bucket("Bash", {"command": "TOKEN=sk-live-zzz"}), "Bash:other")


class Outline(unittest.TestCase):
    def test_python(self):
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write("import os\n\nclass A:\n    def f(self):\n        pass\n\ndef g():\n    return 1\n")
        ol = outline.outline(fh.name)
        self.assertIn("3  class A", ol)
        self.assertIn("7  def g", ol)


class Hooks(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def test_screenshot_gets_scaled(self):
        out = run_hook("pre", {"tool_name": "mcp__Claude_Browser__computer",
                               "tool_input": {"action": "screenshot"}}, self.home)
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["scale"], 0.4)

    def test_repeat_screenshot_denied_until_action(self):
        shot = {"tool_name": "mcp__Claude_Browser__computer", "session_id": "d",
                "tool_input": {"action": "screenshot"}}
        run_hook("pre", shot, self.home)
        out = run_hook("pre", shot, self.home)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        run_hook("pre", {"tool_name": "mcp__Claude_Browser__find", "session_id": "d",
                         "tool_input": {"query": "x"}}, self.home)  # read-only: still denied
        out = run_hook("pre", shot, self.home)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        run_hook("pre", {"tool_name": "mcp__Claude_Browser__navigate", "session_id": "d",
                         "tool_input": {"url": "x"}}, self.home)
        out = run_hook("pre", shot, self.home)
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["scale"], 0.4)
        explicit = dict(shot, tool_input={"action": "screenshot", "scale": 0.5})
        self.assertIsNone(run_hook("pre", explicit, self.home))  # escape hatch

    def test_explicit_scale_and_saved_screenshots_untouched(self):
        self.assertIsNone(run_hook("pre", {"tool_name": "mcp__claude-in-chrome__computer",
                                           "tool_input": {"action": "screenshot", "scale": 1}}, self.home))
        self.assertIsNone(run_hook("pre", {"tool_name": "mcp__claude-in-chrome__computer",
                                           "tool_input": {"action": "screenshot", "save_to_disk": True}}, self.home))

    def test_clicks_untouched(self):
        self.assertIsNone(run_hook("pre", {"tool_name": "mcp__Claude_Browser__computer",
                                           "tool_input": {"action": "left_click", "coordinate": [1, 2]}}, self.home))

    def test_batch_screenshots_scaled(self):
        inp = {"actions": [{"name": "navigate", "input": {"url": "x"}},
                           {"name": "computer", "input": {"action": "screenshot"}}]}
        out = run_hook("pre", {"tool_name": "mcp__Claude_Browser__browser_batch", "tool_input": inp}, self.home)
        acts = out["hookSpecificOutput"]["updatedInput"]["actions"]
        self.assertEqual(acts[1]["input"]["scale"], 0.4)
        self.assertEqual(acts[0], inp["actions"][0])

    def test_batch_earlier_screenshots_smaller(self):
        inp = {"actions": [{"name": "computer", "input": {"action": "screenshot"}},
                           {"name": "computer", "input": {"action": "left_click", "coordinate": [1, 1]}},
                           {"name": "computer", "input": {"action": "screenshot"}}]}
        out = run_hook("pre", {"tool_name": "mcp__claude-in-chrome__browser_batch", "tool_input": inp}, self.home)
        acts = out["hookSpecificOutput"]["updatedInput"]["actions"]
        self.assertEqual((acts[0]["input"]["scale"], acts[2]["input"]["scale"]), (0.25, 0.4))

    def test_big_read_outline_then_allowed(self):
        path = os.path.join(self.home, "big.py")
        with open(path, "w") as fh:
            fh.write("".join("def f%d():\n    return %d\n\n" % (i, i) for i in range(3000)))
        ev = {"tool_name": "Read", "tool_input": {"file_path": path}, "session_id": "s"}
        out = run_hook("pre", ev, self.home)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("def f0", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(run_hook("pre", ev, self.home))  # second ask goes through

    def test_ranged_read_untouched(self):
        path = os.path.join(self.home, "big.txt")
        with open(path, "w") as fh:
            fh.write("x" * 100000)
        self.assertIsNone(run_hook("pre", {"tool_name": "Read", "session_id": "s",
                                           "tool_input": {"file_path": path, "offset": 1, "limit": 50}}, self.home))

    @unittest.skipUnless(sys.platform == "darwin", "uses sips")
    def test_big_image_downscaled(self):
        src = os.path.join(self.home, "a.png")
        subprocess.run(["sips", "-s", "format", "png", "-z", "2000", "3000",
                        "/System/Library/CoreServices/CoreTypes.bundle/Contents/Resources/GenericDocumentIcon.icns",
                        "--out", src], capture_output=True)
        if not os.path.exists(src):
            self.skipTest("no sample image")
        out = run_hook("pre", {"tool_name": "Read", "tool_input": {"file_path": src}}, self.home)
        small = out["hookSpecificOutput"]["updatedInput"]["file_path"]
        dims = subprocess.run(["sips", "-g", "pixelWidth", small], capture_output=True, text=True).stdout
        self.assertIn("1024", dims)

    def test_post_bash_trims(self):
        resp = {"stdout": "\n".join(str(i) for i in range(20000)), "stderr": "", "interrupted": False, "isImage": False}
        out = run_hook("post", {"tool_name": "Bash", "tool_response": resp}, self.home)
        new = out["hookSpecificOutput"]["updatedToolOutput"]
        self.assertEqual(set(new), set(resp))
        self.assertLess(len(new["stdout"]), 7000)

    def test_post_small_bash_untouched(self):
        self.assertIsNone(run_hook("post", {"tool_name": "Bash", "tool_response": {"stdout": "hi", "stderr": ""}}, self.home))

    def test_post_mcp_minifies(self):
        body = json.dumps({"rows": [{"a": i, "b": None} for i in range(300)]}, indent=4)
        out = run_hook("post", {"tool_name": "mcp__x__list", "tool_response": [{"type": "text", "text": body}]}, self.home)
        text = out["hookSpecificOutput"]["updatedToolOutput"][0]["text"]
        self.assertEqual(len(json.loads(text)["rows"]), 300)

    def test_garbage_fails_open(self):
        env = dict(os.environ, HOME=self.home)
        r = subprocess.run(HOOK + ["pre"], input="not json", capture_output=True, text=True, env=env)
        self.assertEqual((r.returncode, r.stdout), (0, ""))


class Install(unittest.TestCase):
    def test_roundtrip_preserves_user_settings(self):
        d = tempfile.mkdtemp()
        cli.SETTINGS = os.path.join(d, "settings.json")
        cli.CLAUDE_MD = os.path.join(d, "CLAUDE.md")
        original = {"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "mine.sh"}]}]},
                    "env": {"FOO": "1"}, "skipWorkflowUsageWarning": True}
        with open(cli.SETTINGS, "w") as fh:
            json.dump(original, fh)
        with open(cli.CLAUDE_MD, "w") as fh:
            fh.write("# my notes\n")
        sys.stdout, real = io.StringIO(), sys.stdout
        try:
            cli.install("max")
            cli.install("strong")  # idempotent: no duplicate hooks
            with open(cli.SETTINGS) as fh:
                s = json.load(fh)
            self.assertEqual(len(s["hooks"]["PreToolUse"]), 1)
            self.assertEqual(s["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "200000")
            cli.uninstall()
        finally:
            sys.stdout = real
        with open(cli.SETTINGS) as fh:
            self.assertEqual(json.load(fh), original)
        with open(cli.CLAUDE_MD) as fh:
            self.assertEqual(fh.read(), "# my notes\n")


def _fake_data():
    import time
    now = time.time()
    return {"profile": "strong", "replay": {"orig": 1_000_000, "trimmed": 900_000, "strong": 250_000},
            "daily": {"2026-01-01": {"claude-opus-5": [1_000_000, 0, 0, 0, 1]}},
            "savings": {"daily": {}},
            "sessions": [{"start": now - 86400 * 60, "end": now, "title": "secret project", "calls": 400,
                          "cr": 10, "sub": [0, 0, 0, 0, 0]}]}


class Share(unittest.TestCase):
    def test_summary_math(self):
        from tokentrim.share import summary
        s = summary(_fake_data())
        self.assertAlmostEqual(s["share"], 0.75)
        self.assertAlmostEqual(s["times"], 4.0)
        self.assertAlmostEqual(s["usd"], 750_000 * 0.5 / 1e6)  # opus cache read = $0.50/M

    def test_card_has_totals_only(self):
        from tokentrim.share import summary, svg
        card = svg(summary(_fake_data()))
        self.assertIn("75% less", card)
        self.assertNotIn("secret project", card)


class Pro(unittest.TestCase):
    def test_free_user_is_gated(self):
        from tokentrim import pro
        with tempfile.TemporaryDirectory() as d:
            old, pro.LICENSE = pro.LICENSE, os.path.join(d, "license.json")
            try:
                self.assertFalse(pro.is_pro())
                with self.assertRaises(SystemExit):
                    pro.require()
            finally:
                pro.LICENSE = old

    def test_stale_license_revalidates_with_polar(self):
        from tokentrim import pro
        with tempfile.TemporaryDirectory() as d:
            saved = pro.LICENSE, pro.ORG_ID, pro._post
            pro.LICENSE, pro.ORG_ID = os.path.join(d, "license.json"), "org-1"
            try:
                pro._save({"key": "k", "instance": "a", "checked": 0})
                pro._post = lambda action, f: {"status": "granted", "organization_id": "org-1"}
                self.assertTrue(pro.is_pro())
                pro._save({"key": "k", "instance": "a", "checked": 0})
                pro._post = lambda action, f: {"status": "granted", "organization_id": "someone-else"}
                self.assertFalse(pro.is_pro())
            finally:
                pro.LICENSE, pro.ORG_ID, pro._post = saved

    def test_recent_license_works_offline(self):
        import time
        from tokentrim import pro
        with tempfile.TemporaryDirectory() as d:
            old, pro.LICENSE = pro.LICENSE, os.path.join(d, "license.json")
            try:
                pro._save({"key": "k", "instance": "i", "checked": time.time()})
                self.assertTrue(pro.is_pro())  # inside RECHECK: no network call
            finally:
                pro.LICENSE = old

    def test_week_counts_long_sessions(self):
        from tokentrim import pro
        self.assertEqual(pro._week(_fake_data())["long"], 1)

if __name__ == "__main__":
    unittest.main()
