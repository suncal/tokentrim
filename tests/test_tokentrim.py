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


def run_hook(event, payload, home, *extra):
    env = dict(os.environ, HOME=home)
    r = subprocess.run(HOOK + [event] + list(extra), input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=30)
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

    def test_scale_cap_off_by_default_then_lowers_explicit_scale(self):
        shot = {"tool_name": "mcp__Claude_Browser__computer", "tool_input": {"action": "screenshot", "scale": 0.6}}
        self.assertIsNone(run_hook("pre", shot, self.home))
        path = os.path.join(self.home, ".config/tokentrim/config.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"screenshot_scale_cap": 0.4}, fh)
        out = run_hook("pre", shot, self.home)
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["scale"], 0.4)
        zoom = {"tool_name": "mcp__Claude_Browser__computer",
                "tool_input": {"action": "zoom", "region": [0, 0, 9, 9], "scale": 0.9}}
        self.assertIsNone(run_hook("pre", zoom, self.home))
        inp = {"actions": [{"name": "computer", "input": {"action": "screenshot", "scale": 0.5}}]}
        out = run_hook("pre", {"tool_name": "mcp__Claude_Browser__browser_batch", "tool_input": inp}, self.home)
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["actions"][0]["input"]["scale"], 0.4)

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


class Plugin(unittest.TestCase):
    SHOT = {"tool_name": "mcp__Claude_Browser__computer", "tool_input": {"action": "screenshot"}}

    def test_plugin_hook_acts_without_settings_install(self):
        out = run_hook("pre", self.SHOT, tempfile.mkdtemp(), "--plugin")
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["scale"], 0.4)

    def test_plugin_hook_stands_down_when_settings_installed(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".claude"))
        with open(os.path.join(home, ".claude", "settings.json"), "w") as fh:
            json.dump({"hooks": {k: [v] for k, v in cli._entries().items()}}, fh)
        self.assertIsNone(run_hook("pre", self.SHOT, home, "--plugin"))
        self.assertIsNotNone(run_hook("pre", self.SHOT, home))  # the settings hook itself still runs

    def test_plugin_hooks_match_install(self):
        with open(os.path.join(ROOT, "hooks", "hooks.json")) as fh:
            plugin = json.load(fh)["hooks"]
        for event, entry in cli._entries().items():
            self.assertEqual(plugin[event][0].get("matcher", ""), entry["matcher"])
            self.assertIn("--plugin", plugin[event][0]["hooks"][0]["command"])


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

    def test_budget_alert_once_per_level(self):
        from tokentrim import pro
        sent = []
        with tempfile.TemporaryDirectory() as d:
            saved = os.environ.get("HOME"), pro._notify
            os.environ["HOME"], pro._notify = d, sent.append
            try:
                self.assertFalse(pro._budget_alert({"tokens": 70}, 100))   # under 80%
                self.assertTrue(pro._budget_alert({"tokens": 85}, 100))
                self.assertFalse(pro._budget_alert({"tokens": 90}, 100))   # already told this week
                self.assertTrue(pro._budget_alert({"tokens": 120}, 100))   # passing 100% tells again
                self.assertFalse(pro._budget_alert({"tokens": 130}, 100))
            finally:
                os.environ["HOME"], pro._notify = saved
        self.assertEqual(len(sent), 2)
        self.assertIn("85%", sent[0])

    def test_slack_post_reports_failures(self):
        import http.server
        import threading
        from tokentrim import pro
        got = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                code, body = (200, b"ok") if self.path == "/good" else (404, b"no_service")
                self.send_response(code)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % srv.server_port
        try:
            self.assertIsNone(pro._slack_post(base + "/good", "hi"))
            self.assertIn("no_service", pro._slack_post(base + "/bad", "hi"))
            self.assertEqual(got[0], {"text": "hi"})
        finally:
            srv.shutdown()

    def test_slack_rejects_non_webhook(self):
        from tokentrim import pro
        old, pro.require = pro.require, lambda: None
        try:
            with self.assertRaises(SystemExit):
                pro.slack("https://example.com/hook")
        finally:
            pro.require = old


class LiveTest(unittest.TestCase):
    def test_counterfactual_keeps_history_and_trims(self):
        from tokentrim.live import Conv
        c = Conv("s", "p")
        self.assertEqual(c.step(0, 100_000, 850_000), 0)
        c.pending = 5_000                       # a trimmed tool result lands
        self.assertEqual(c.step(1, 110_000, 850_000), 5_000)
        self.assertEqual(c.step(2, 40_000, 850_000), 115_000 - 40_000)  # early summary: without tokentrim keeps it all
        self.assertEqual(c.compactions, 1)
        c.step(3, 50_000, 120_000)              # past the native limit: Claude Code would have compacted too
        self.assertEqual(c.cf, 50_000)


class SmartTest(unittest.TestCase):
    """Daily budget, cold cache and model advisor (hook side), smart settings (controls side). All in a temp HOME."""
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def _write(self, rel, obj):
        path = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(obj, fh)

    def _today(self, cost, ttl=300):
        import time
        self._write(".cache/tokentrim/today.json", {"day": time.strftime("%Y-%m-%d"), "ts": time.time(), "cost": cost, "ttl": ttl, "price_in": 5})

    def _transcript(self, ctx, model, ago):
        import time
        path = os.path.join(self.home, "t.jsonl")
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - ago))
        with open(path, "w") as fh:
            fh.write(json.dumps({"type": "assistant", "timestamp": ts, "message": {"model": model, "usage": {
                "input_tokens": 10, "cache_read_input_tokens": ctx, "cache_creation_input_tokens": 0, "output_tokens": 5}}}) + "\n")
        return path

    def test_budget_blocks_unless_overridden(self):
        self._write(".config/tokentrim/config.json", {"budget_guard": True, "budget_usd": 10, "budget_block": True})
        self._today(12.5)
        out = run_hook("prompt", {"prompt": "add a test", "session_id": "b"}, self.home)
        self.assertEqual(out["decision"], "block")
        self.assertIn("$10", out["reason"])
        out = run_hook("prompt", {"prompt": "++ add a test", "session_id": "b"}, self.home)
        self.assertNotEqual((out or {}).get("decision"), "block")

    def test_budget_warns_without_block(self):
        self._write(".config/tokentrim/config.json", {"budget_guard": True, "budget_usd": 10})
        self._today(8.5)
        out = run_hook("prompt", {"prompt": "go", "session_id": "w"}, self.home)
        self.assertIn("budget", out["systemMessage"])
        self.assertIsNone(run_hook("prompt", {"prompt": "go", "session_id": "w"}, self.home))   # said once

    def test_budget_says_when_paused(self):
        self._write(".config/tokentrim/config.json", {"budget_guard": True, "budget_usd": 10, "budget_block": True})
        out = run_hook("prompt", {"prompt": "go", "session_id": "p"}, self.home)   # no today.json: tracker not running
        self.assertIn("paused", out["systemMessage"])
        self.assertIsNone(run_hook("prompt", {"prompt": "go", "session_id": "p"}, self.home))   # once a day, never blocks

    def test_cold_cache_after_long_pause(self):
        self._today(1, ttl=300)
        t = self._transcript(120_000, "claude-sonnet-4-5", ago=20 * 60)
        out = run_hook("prompt", {"prompt": "continue", "session_id": "c", "transcript_path": t}, self.home)
        self.assertIn("cache has expired", out["systemMessage"])
        t = self._transcript(120_000, "claude-sonnet-4-5", ago=60)
        out = run_hook("prompt", {"prompt": "continue", "session_id": "c2", "transcript_path": t}, self.home) or {}
        self.assertNotIn("cache has expired", out.get("systemMessage", ""))   # still warm

    def test_advisor_on_quick_opus_job_only(self):
        t = self._transcript(10_000, "claude-opus-4-1", ago=30)
        out = run_hook("prompt", {"prompt": "fix the typo in the readme", "session_id": "a", "transcript_path": t}, self.home)
        self.assertIn("/model sonnet", out["systemMessage"])
        out = run_hook("prompt", {"prompt": "debug why the cache rebuilds", "session_id": "a2", "transcript_path": t}, self.home)
        self.assertIsNone(out)

    def test_claude_settings_remember_and_restore(self):
        from tokentrim import controls
        s = {"model": "opus"}
        controls._claude_set(s, "model_mode", "auto")
        self.assertEqual(s["model"], "opusplan")
        controls._claude_set(s, "model_mode", "sonnet")
        self.assertEqual(s["tokentrim"]["prev"]["model"], "opus")   # the user's own value, not ours
        controls._claude_set(s, "model_mode", "off")
        self.assertEqual(s["model"], "opus")
        controls._claude_set(s, "subagent_model", "haiku")
        self.assertEqual(s["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], "haiku")
        controls._claude_set(s, "subagent_model", "off")
        self.assertNotIn("env", s)

    def test_off_keeps_a_model_picked_later(self):
        from tokentrim import controls
        s = {"model": "opus"}
        controls._claude_set(s, "model_mode", "auto")
        s["model"] = "haiku"                      # the user picked a model with /model afterwards
        controls._claude_set(s, "model_mode", "off")
        self.assertEqual(s["model"], "haiku")

    def test_support_names_old_versions(self):
        from tokentrim import controls
        sup = controls.support({"app": "2.1.299", "cli": "2.1.69"})
        self.assertIsNone(sup["model_mode"])
        self.assertIn("2.1.69", sup["cache_ttl"])
        self.assertTrue(controls.support({"cli": "2.1.69"})["cache_ttl"].startswith("Needs"))

    def test_validate_rejects_bad_choices(self):
        from tokentrim import controls
        for bad in ({"model_mode": "opus"}, {"budget_usd": 7}, {"budget_usd": True}, {"effort": "max"}):
            with self.assertRaises(ValueError):
                controls.validate(bad)
        self.assertEqual(controls.validate({"budget_usd": 20, "model_mode": "auto"}), {"budget_usd": 20, "model_mode": "auto"})

    def test_uninstall_restores_users_model(self):
        from tokentrim import controls
        d = tempfile.mkdtemp()
        old = cli.SETTINGS, cli.CLAUDE_MD
        cli.SETTINGS, cli.CLAUDE_MD = os.path.join(d, "settings.json"), os.path.join(d, "CLAUDE.md")
        with open(cli.SETTINGS, "w") as fh:
            json.dump({"model": "opus", "effortLevel": "high"}, fh)
        sys.stdout, real = io.StringIO(), sys.stdout
        try:
            cli.install("strong")
            s = cli._load_settings()
            controls._claude_set(s, "model_mode", "auto")
            controls._claude_set(s, "effort", "low")
            cli._save_settings(s)
            cli.uninstall()
            with open(cli.SETTINGS) as fh:
                s = json.load(fh)
            self.assertEqual((s["model"], s["effortLevel"]), ("opus", "high"))
        finally:
            sys.stdout = real
            cli.SETTINGS, cli.CLAUDE_MD = old


if __name__ == "__main__":
    unittest.main()
