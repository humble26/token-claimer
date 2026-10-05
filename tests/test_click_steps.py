# -*- coding: utf-8 -*-
r"""多步点击（步骤序列）的回归测试。

背景：TraeWork CN 的领取入口是两步 —— 先点开左下角账户菜单，菜单里才会
出现「每日领 100 积分」。原来的引擎"每次只点一个元素、点到即完成"，最多
只能把菜单点开，领不到积分。

这里覆盖状态机的四个关键行为：
  1. 前进：某一步点成功后等 wait 秒再走下一步，重试预算重置。
  2. 收尾：最后一步点成功（或检测到「今日已签」）才算完成。
  3. 跳步：前置步骤就撞上完成标识 → 说明菜单开着，直接跳到最后一步确认，
     不要傻乎乎再点一次前置按钮把菜单关掉（toggle）。
  4. 探测：最后一步的目标已经可见时同样跳步；单步应用不该走这条路径。

真实点击依赖 COM/UIA，测试里把 uia_click.find_and_click 换成脚本化的假实现，
这样状态机本身可以脱离 Windows 界面单独验证。

运行：python -m unittest discover -s tests
"""

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for name in ("tkinter", "tkinter.filedialog", "tkinter.messagebox", "tkinter.ttk"):
    if name not in sys.modules:
        mod = types.ModuleType(name)
        if name == "tkinter":
            for attr in ("Tk", "Toplevel", "StringVar", "BooleanVar", "IntVar"):
                setattr(mod, attr, object)
        sys.modules[name] = mod

import token_claimer as tc  # noqa: E402

APP = {"key": "traework", "label": "TraeWork CN", "exe": r"C:\x\Y.exe"}
TWO_STEPS = [{"desc": "开菜单", "keywords": ["用户"], "wait": 2.0},
             {"desc": "点领取", "keywords": ["每日领"], "wait": 0.0}]
NEVER_EXPIRE = 9e9     # 让用例专注于"点没点、走到哪一步"，不受 deadline 干扰


class _StubCfg:
    def __init__(self, **over):
        self.data = {
            "click_enabled": False,
            "click_keywords": list(tc.CLICK_KEYWORDS_DEFAULT),
            "per_app_click": {},
            "click_wait_window": 25,
            "click_retry_seconds": 15,
            "click_max_attempts": 10,
            "keep_minutes": 10,
            "restart_if_no_tree": True,
        }
        self.data.update(over)

    def save(self):
        pass


class _FakeUia:
    """脚本化的假 find_and_click：探测与正式点击各有一条队列。"""

    def __init__(self, script, probe_script=None):
        self.script = list(script)
        self.probe_script = list(probe_script) if probe_script else None
        self.calls = []

    def __call__(self, image, keywords, negative=(), clicked_names=None,
                 point_pct=None, region_pct=None, probe=False):
        self.calls.append({"keywords": list(keywords), "probe": probe,
                           "point": point_pct, "region": region_pct})
        if probe:
            if self.probe_script:
                return self.probe_script.pop(0)
            return "not-found", "目标不可见"      # 默认：菜单没开着
        if not self.script:
            raise AssertionError("假实现被多调用了一次，脚本已用尽")
        return self.script.pop(0)

    def normal_calls(self):
        return [c for c in self.calls if not c["probe"]]

    def probed(self):
        return any(c["probe"] for c in self.calls)


class _EngineCase(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.cfg = _StubCfg(per_app_click={"traework": {"steps": TWO_STEPS}})
        self.eng = tc.ClaimEngine(
            self.cfg, log=lambda m, tag="info": self.logs.append(m))

    def _install(self, script, probe_script=None):
        fake = _FakeUia(script, probe_script)
        orig = tc.uia_click.find_and_click
        tc.uia_click.find_and_click = fake
        self.addCleanup(lambda: setattr(tc.uia_click, "find_and_click", orig))
        self.fake = fake
        return fake

    def _job(self, **over):
        job = self.eng.make_click_job(APP, will_launch=False, start_ts=1000.0)
        job["deadline"] = NEVER_EXPIRE
        job.update(over)
        return job

    def _single_step_job(self):
        # Trae CN 没有预置覆盖，代表"单步应用"
        self.cfg.data["per_app_click"] = {}
        job = self.eng.make_click_job(
            {"key": "trae", "label": "Trae CN", "exe": r"C:\x\Z.exe"},
            will_launch=False, start_ts=1000.0)
        job["deadline"] = NEVER_EXPIRE
        return job

    def _joined(self):
        return "\n".join(self.logs)


class MakeJobTest(_EngineCase):
    def test_job_carries_normalized_steps(self):
        job = self._job()
        self.assertEqual([s["desc"] for s in job["steps"]], ["开菜单", "点领取"])
        self.assertEqual(job["step"], 0)
        self.assertFalse(job["done"])

    def test_single_step_app_gets_one_step(self):
        job = self._single_step_job()
        self.assertEqual(len(job["steps"]), 1)
        self.assertEqual(job["steps"][0]["keywords"],
                         list(tc.CLICK_KEYWORDS_DEFAULT))

    def test_deadline_scales_with_step_count(self):
        """keep=0 时按步数放大总预算，否则两步入口走不完第一步就超时。"""
        self.cfg.data["keep_minutes"] = 0
        self.cfg.data["per_app_click"] = {}
        one = self.eng.make_click_job(
            {"key": "trae", "label": "Z", "exe": r"C:\x\Z.exe"},
            will_launch=False, start_ts=1000.0)
        two = self.eng.make_click_job(APP, will_launch=False, start_ts=1000.0)
        self.assertGreater(two["deadline"], one["deadline"])


class AdvanceTest(_EngineCase):
    def test_first_step_click_advances_without_finishing(self):
        self._install([("clicked", "“用户0433459512”")])
        job = self._job()
        self.eng.process_click_job(job, 2000.0)
        self.assertEqual(job["step"], 1)
        self.assertFalse(job["done"], "第一步点完不能算领取完成")
        self.assertEqual(job["attempts"], 0, "进入下一步时重试预算要重置")
        self.assertEqual(job["next"], 2002.0, "要留出 wait 秒等菜单渲染")
        self.assertIn("继续下一步", self._joined())

    def test_last_step_click_finishes(self):
        self._install([("clicked", "“用户0433459512”"),
                       ("clicked", "“每日领 100 积分”")])
        job = self._job()
        self.eng.process_click_job(job, 2000.0)
        self.eng.process_click_job(job, 2002.0)
        self.assertTrue(job["done"])
        self.assertIn("已自动点击", self._joined())

    def test_step_waits_for_render_before_next_attempt(self):
        self._install([("clicked", "“用户”")])
        job = self._job()
        self.eng.process_click_job(job, 2000.0)
        self.eng.process_click_job(job, 2001.0)      # 还没到 2002
        self.assertEqual(job["step"], 1)
        self.assertFalse(job["done"])
        self.assertEqual(len(self.fake.normal_calls()), 1)


class AlreadyTest(_EngineCase):
    def test_already_on_first_step_jumps_to_last(self):
        """菜单开着且今天已领：不该再点一次前置按钮把菜单关掉。"""
        self._install([("already", "检测到已完成领取的标识")])
        job = self._job()
        self.eng.process_click_job(job, 2000.0)
        self.assertEqual(job["step"], 1)
        self.assertFalse(job["done"], "跳步后还要最后一步确认一次")
        self.assertIn("跳到领取步骤确认", self._joined())

    def test_already_on_last_step_finishes(self):
        self._install([("clicked", "“用户”"),
                       ("already", "检测到已完成领取的标识")])
        job = self._job()
        self.eng.process_click_job(job, 2000.0)
        self.eng.process_click_job(job, 2002.0)
        self.assertTrue(job["done"])
        self.assertIn("无需再点", self._joined())


class ProbeTest(_EngineCase):
    def test_probe_hit_skips_prefix_steps(self):
        """探测到最后一步目标已可见 → 直接领取，不碰前置按钮。"""
        self._install([("clicked", "“每日领 100 积分”")],
                      probe_script=[("found", "“每日领 100 积分”已可见")])
        job = self._job()
        self.eng.process_click_job(job, 2000.0)   # 探测命中，跳到最后一步
        self.assertEqual(job["step"], 1)
        self.assertFalse(job["done"], "跳步本身不算完成")
        self.eng.process_click_job(job, 2001.0)   # 下一秒执行领取
        self.assertTrue(job["done"])
        self.assertEqual(self.fake.calls[0]["keywords"], ["每日领"])
        self.assertEqual([c["keywords"] for c in self.fake.normal_calls()],
                         [["每日领"]], "不该点前置步骤")
        self.assertIn("跳过前置步骤", self._joined())

    def test_probe_miss_falls_back_to_normal_order(self):
        self._install([("clicked", "“用户”")])
        job = self._job()
        self.eng.process_click_job(job, 2000.0)
        self.assertEqual(job["step"], 1)
        self.assertEqual(self.fake.normal_calls()[0]["keywords"], ["用户"])

    def test_single_step_never_probes(self):
        self._install([("clicked", "“领取”")])
        job = self._single_step_job()
        self.eng.process_click_job(job, 2000.0)
        self.assertTrue(job["done"])
        self.assertFalse(self.fake.probed())

    def test_point_based_last_step_never_probes(self):
        """坐标步骤查不了可见性，别浪费一次探测。"""
        self.cfg.data["per_app_click"] = {"traework": {"steps": [
            {"desc": "开菜单", "keywords": ["用户"]},
            {"desc": "点坐标", "point": [16.0, 97.0]}]}}
        self._install([("clicked", "“用户”")])
        job = self._job()
        self.eng.process_click_job(job, 2000.0)
        self.assertFalse(self.fake.probed())
        self.assertEqual(job["step"], 1)


class GiveUpTest(_EngineCase):
    def test_giving_up_on_prefix_step_reports_which_step(self):
        self.cfg.data["click_max_attempts"] = 3
        self._install([("no-element", "未找到匹配按钮")] * 3)
        job = self._job()
        for i in range(3):
            self.eng.process_click_job(job, 2000.0 + i * 20)
        self.assertTrue(job["done"])
        self.assertIn("开菜单", self._joined(), "日志要说明是哪一步没点到")
        self.assertIn("放弃自动点击", self._joined())

    def test_no_element_retries_before_giving_up(self):
        self.cfg.data["click_max_attempts"] = 3
        self._install([("no-element", "未找到匹配按钮")])
        job = self._job()
        self.eng.process_click_job(job, 2000.0)
        self.assertFalse(job["done"])
        self.assertEqual(job["next"], 2000.0 + job["fast_retry"])
        self.assertEqual(job["step"], 0, "重试期间应停留在同一步")

    def test_no_window_keeps_retrying(self):
        self._install([("no-window", "可见窗口未出现")])
        job = self._job()
        self.eng.process_click_job(job, 2000.0)
        self.assertFalse(job["done"])
        self.assertEqual(job["step"], 0)

    def test_no_element_stops_at_the_short_budget(self):
        """树是活的但没这个按钮时，别按 click_max_attempts 转满一分多钟。

        实测 ZCode 的 banner 已隐藏时转了 78 秒才放弃。这类失败没有"再等等就好"
        的成分：树正常，按钮在不在几秒内就能定性，所以用更短的预算收手。
        """
        self.cfg.data["click_max_attempts"] = 10
        self._install([("no-element", "未找到匹配按钮")] * 10)
        job = self._job()
        for i in range(10):
            self.eng.process_click_job(job, 2000.0 + i * 20)
            if job["done"]:
                break
        self.assertTrue(job["done"])
        self.assertEqual(job["attempts"], tc.NO_ELEMENT_MAX_ATTEMPTS)
        self.assertLess(tc.NO_ELEMENT_MAX_ATTEMPTS, 10,
                        "这条预算必须比通用的点击重试上限短，否则等于没改")


class MinimizedAndNoTreeTest(_EngineCase):
    r"""窗口最小化 / 界面树为空时的状态处理。

    背景：uia_click 在窗口最小化、矩形不可用时返回 minimized（见
    MinimizedWindowGuardTest），但引擎当时没有对应分支，落到 else 被记成
    "自动点击出错"并立即放弃 —— 用户看到的是"出错"，实际只需还原窗口就能继续。

    no-tree 则相反：它没有放弃条件，界面树又不会自己长出来，于是任务永远留在
    队列里。keep=0 时 run_headless 的 `while jobs or closes` 因此永不退出。
    """

    def test_minimized_retries_instead_of_giving_up(self):
        self._install([("minimized", "目标窗口已最小化，无法按区域查找元素")])
        job = self._job()
        self.eng.process_click_job(job, 2000.0)
        self.assertFalse(job["done"], "最小化是可恢复状态，应先重试")
        self.assertEqual(job["step"], 0)
        self.assertEqual(job["next"], 2000.0 + job["fast_retry"])
        self.assertNotIn("自动点击出错", self._joined())

    def test_minimized_gives_up_after_budget_with_actionable_hint(self):
        self.cfg.data["click_max_attempts"] = 3
        self._install([("minimized", "目标窗口已最小化")] * 3)
        job = self._job()
        for i in range(3):
            self.eng.process_click_job(job, 2000.0 + i * 20)
        self.assertTrue(job["done"])
        self.assertIn("还原窗口", self._joined(), "放弃时要说清怎么办")

    def test_no_tree_gives_up_after_budget_when_restart_is_off(self):
        """关掉自愈重启后，界面树不会自己长出来，只能按预算放弃。"""
        self.cfg.data["click_max_attempts"] = 3
        self.cfg.data["restart_if_no_tree"] = False
        self._install([("no-tree", "界面树没有内容")] * 3)
        job = self._job()
        for i in range(3):
            self.eng.process_click_job(job, 2000.0 + i * 20)
        self.assertTrue(job["done"], "界面树不会自己长出来，不能无限重试")

    def test_no_tree_retry_hint_is_logged_only_once(self):
        self.cfg.data["restart_if_no_tree"] = False
        self._install([("no-tree", "界面树没有内容")] * 2)
        job = self._job()
        self.eng.process_click_job(job, 2000.0)
        self.eng.process_click_job(job, 2000.0 + job["retry"])
        self.assertEqual(sum("界面树没有内容" in m for m in self.logs), 1)


class NoTreeRecoverTest(_EngineCase):
    r"""界面树不可用时的自愈：优雅关闭 → 等退出（超时强杀）→ 带参数冷启动。

    背景（2026-10-04 线上故障）：WorkBuddy 明明带着 --force-renderer-accessibility
    在跑、窗口也没最小化，界面树却只剩 8 个无名 Pane，tree_named < 4 被判成
    no-tree。实测 WM_GETOBJECT（含正确签名的 OBJID_CLIENT）与 oleacc 都无法把
    树叫回来 —— 树一旦塌陷就是永久的。日志里"已在运行，跳过启动"之后必然跟
    一条 no-tree，而冷启动的实例三步都能点通，所以唯一可靠的恢复手段是冷启动。

    这里覆盖状态机的关键行为：何时才重启（区分"树还在长"和"永远不会好"）、
    重启前必须确认旧进程真的退出（否则单实例锁让新进程拿不到无障碍参数）、
    以及重启后的复位与预算上限。
    """

    def setUp(self):
        super().setUp()
        self.proc = {"graceful": [], "force": [], "launch": []}
        self.running: list[bool] = []
        self.eng.is_running = lambda image: (
            self.running.pop(0) if self.running else False)
        self.eng.close_graceful = lambda image: (
            self.proc["graceful"].append(image) or True)
        self.eng.close_force = lambda image: (
            self.proc["force"].append(image) or True)
        self.eng.launch = lambda exe, args=(): self.proc["launch"].append(
            (exe, tuple(args)))

    def _advance(self, job, now):
        """把时间推到任务的下一个动作点，执行一次。"""
        now = max(now, job["next"])
        self.eng.process_click_job(job, now)
        return now

    def _step_until(self, job, cond, now=2000.0, limit=30):
        for _ in range(limit):
            if cond(job):
                return now
            now = self._advance(job, now)
        raise AssertionError("条件在限定步数内未满足")

    # ---- 何时触发重启 ----
    def test_persistent_no_tree_restarts_a_running_instance(self):
        self._install([("no-tree", "界面树没有内容")] * 30)
        job = self._job()          # will_launch=False → 等 20 秒
        self._step_until(job, lambda j: j["recover_stage"] is not None)
        self.assertEqual(job["recover_stage"], 0)
        self.assertIn("尝试重启客户端", self._joined())

    def test_first_no_tree_does_not_restart_immediately(self):
        """刚启动的实例树还在长，第一次 no-tree 不该立刻重启把它打断。"""
        self._install([("no-tree", "界面树没有内容")] * 30)
        job = self._job()
        self.eng.process_click_job(job, 2000.0)
        self.assertIsNone(job["recover_stage"])
        self.assertEqual(self.proc["graceful"], [])

    def test_tool_launched_instance_waits_longer(self):
        launched = self.eng.make_click_job(APP, will_launch=True, start_ts=1000.0)
        running = self.eng.make_click_job(APP, will_launch=False, start_ts=1000.0)
        self.assertEqual(launched["no_tree_wait"], tc.NO_TREE_WAIT_LAUNCHED)
        self.assertEqual(running["no_tree_wait"], tc.NO_TREE_WAIT_RUNNING)
        self.assertGreater(tc.NO_TREE_WAIT_LAUNCHED, tc.NO_TREE_WAIT_RUNNING)

    def test_tree_recovery_clears_the_unavailable_timer(self):
        self._install([("no-tree", "界面树没有内容"), ("clicked", "“用户”")])
        job = self._job()
        self.eng.process_click_job(job, 2000.0)
        self.assertEqual(job["no_tree_since"], 2000.0)
        self.eng.process_click_job(job, 2000.0 + job["retry"])
        self.assertEqual(job["no_tree_since"], 0.0, "树恢复后要重新计时")

    # ---- 状态机 ----
    def test_graceful_close_then_relaunch_with_accessibility_flag(self):
        job = self._job()
        job["recover_stage"], job["recover_at"] = 0, 2000.0
        self.running = [True]
        self.eng._process_recover(job, 2000.0)
        self.assertEqual(self.proc["graceful"], ["Y.exe"])
        self.assertEqual(job["recover_stage"], 1)
        self.running = [True]                       # 还没退出
        self.eng._process_recover(job, 2002.0)
        self.assertEqual(job["recover_stage"], 1)
        self.assertEqual(self.proc["force"], [], "没到超时不该强杀")
        self.running = [False]                      # 已优雅退出
        self.eng._process_recover(job, 2004.0)
        self.assertEqual(job["recover_stage"], 3)
        self.assertEqual(self.proc["launch"], [], "要先歇一下再拉起")
        self.eng._process_recover(job, 2004.0 + tc.RECOVER_SETTLE)
        self.assertEqual(self.proc["launch"],
                         [(r"C:\x\Y.exe", (tc.uia_click.A11Y_FLAG,))])
        self.assertIsNone(job["recover_stage"])

    def test_relaunch_resets_steps_and_clicked_names(self):
        job = self._job()
        job["step"], job["attempts"] = 1, 7
        job["clicked"] = {"“用户”"}
        job["recover_stage"], job["recover_at"] = 3, 2000.0
        self.eng._process_recover(job, 2000.0)
        self.assertEqual(job["step"], 0, "新实例的菜单是关着的，前置步骤要重跑")
        self.assertEqual(job["attempts"], 0)
        self.assertEqual(job["clicked"], set())
        self.assertEqual(job["next"], 2000.0 + tc.START_DELAY,
                         "重启后不必再死等窗口等待，退避重试会接住")

    def test_force_kill_when_graceful_close_times_out(self):
        job = self._job()
        job["recover_stage"], job["recover_at"] = 1, 2000.0
        self.running = [True]
        self.eng._process_recover(job, 2000.0 + tc.RECOVER_CLOSE_TIMEOUT)
        self.assertEqual(self.proc["force"], ["Y.exe"],
                         "旧进程不退，新实例就拿不到无障碍参数")
        self.assertEqual(job["recover_stage"], 2)
        self.assertIn("强制结束", self._joined())

    def test_gives_up_when_process_survives_force_kill(self):
        job = self._job()
        job["recover_stage"], job["recover_at"] = 2, 2000.0
        self.running = [True]
        self.eng._process_recover(job, 2000.0 + tc.RECOVER_KILL_TIMEOUT)
        self.assertTrue(job["done"])
        self.assertIn("自动恢复失败", self._joined())

    def test_relaunch_failure_ends_the_job_instead_of_hanging(self):
        job = self._job()
        job["recover_stage"], job["recover_at"] = 3, 2000.0
        self.eng.launch = lambda exe, args=(): (_ for _ in ()).throw(
            OSError("拒绝访问"))
        self.eng._process_recover(job, 2000.0)
        self.assertTrue(job["done"])
        self.assertIsNone(job["recover_stage"])

    def test_relaunch_registers_auto_close_via_callback(self):
        seen = []
        self.eng.on_relaunch = seen.append
        job = self._job()
        job["recover_stage"], job["recover_at"] = 3, 2000.0
        self.eng._process_recover(job, 2000.0)
        self.assertEqual(seen, ["Y.exe"])

    # ---- 预算与开关 ----
    def test_restart_budget_is_finite(self):
        job = self._job()
        job["restarts"] = job["max_restarts"]
        job["no_tree_since"] = 1000.0
        self.assertFalse(self.eng._maybe_recover(job, 5000.0, "界面树没有内容"),
                         "预算用尽后不能再重启，否则会无限重启")

    def test_recover_extends_deadline(self):
        """恢复要花一两分钟，不能算进放弃预算，否则刚重启完就超时放弃。"""
        job = self._job()
        before = job["deadline"]
        job["no_tree_since"] = 1000.0
        self.assertTrue(self.eng._maybe_recover(job, 5000.0, "界面树没有内容"))
        self.assertEqual(job["deadline"], before + tc.RECOVER_BUDGET)

    def test_disabled_switch_never_touches_the_client(self):
        self.cfg.data["restart_if_no_tree"] = False
        self._install([("no-tree", "界面树没有内容")] * 30)
        job = self._job()
        self.assertEqual(job["max_restarts"], 0)
        now = 2000.0
        for _ in range(15):
            now = self._advance(job, now)
        self.assertEqual(self.proc["graceful"], [])
        self.assertEqual(self.proc["force"], [])
        self.assertEqual(self.proc["launch"], [])
        self.assertTrue(job["done"], "关掉开关就回到「按预算放弃」的老行为")


class RegionTest(_EngineCase):
    r"""区域过滤必须真的传到 find_and_click。

    背景：region_pct 加进 uia_click 之后，click_steps_for 归一化步骤时把它
    丢掉了，process_click_job 也没往下传 —— 区域过滤是死代码，关键词仍在
    整个窗口里匹配，而 TraeWork CN 的聊天正文里恰好有"用户""领取"这些字，
    会点到对话上。这里把"配置 → 步骤 → 调用"整条链路钉住。
    """

    def test_builtin_traework_first_step_is_regioned(self):
        """预置的第一步必须带区域，否则"用户"会命中聊天正文。

        注意要用"没有用户覆盖"的配置：本类的 setUp 给 traework 塞了自定义
        steps，会整体顶掉预置写法（见 per_app_click_cfg 的谁出现谁说了算）。
        """
        eng = tc.ClaimEngine(_StubCfg(per_app_click={}),
                             log=lambda m, tag="info": None)
        steps = eng.click_steps_for({"key": "traework"})
        self.assertIsNotNone(steps[0]["region_pct"])
        self.assertIsNotNone(steps[-1]["keywords"])

    def test_config_region_survives_normalization(self):
        self.cfg.data["per_app_click"] = {"traework": {"steps": [
            {"desc": "开菜单", "keywords": ["用户"], "region_pct": [0, 92, 25, 100]},
            {"desc": "点领取", "keywords": ["每日领"]}]}}
        steps = self.eng.click_steps_for({"key": "traework"})
        self.assertEqual(steps[0]["region_pct"], (0.0, 92.0, 25.0, 100.0))
        self.assertIsNone(steps[1]["region_pct"], "没配的步骤不该凭空有区域")

    def test_single_step_region_survives_normalization(self):
        self.cfg.data["per_app_click"] = {
            "workbuddy": {"keywords": ["加油站"], "region_pct": [50, 0, 100, 30]}}
        steps = self.eng.click_steps_for({"key": "workbuddy"})
        self.assertEqual(steps[0]["region_pct"], (50.0, 0.0, 100.0, 30.0))

    def test_broken_region_is_dropped_not_fatal(self):
        """手写配置写错了：当没配处理，别把整次领取打断。"""
        for bad in ([0, 92, 25], "左下角", [0, 100, 25, 92], []):
            with self.subTest(bad=bad):
                self.cfg.data["per_app_click"] = {
                    "traework": {"keywords": ["用户"], "region_pct": bad}}
                steps = self.eng.click_steps_for({"key": "traework"})
                self.assertIsNone(steps[0]["region_pct"])

    def test_click_call_carries_region(self):
        self.cfg.data["per_app_click"] = {"traework": {"steps": [
            {"desc": "开菜单", "keywords": ["用户"], "region_pct": [0, 92, 25, 100]},
            {"desc": "点领取", "keywords": ["每日领"]}]}}
        self._install([("clicked", "“用户”")])
        job = self._job()
        self.eng.process_click_job(job, 2000.0)
        self.assertEqual(self.fake.normal_calls()[0]["region"],
                         (0.0, 92.0, 25.0, 100.0))

    def test_probe_carries_last_step_region(self):
        self.cfg.data["per_app_click"] = {"traework": {"steps": [
            {"desc": "开菜单", "keywords": ["用户"], "region_pct": [0, 92, 25, 100]},
            {"desc": "点领取", "keywords": ["每日领"],
             "region_pct": [0, 60, 25, 95]}]}}
        self._install([("clicked", "“每日领”")])
        job = self._job()
        self.eng.process_click_job(job, 2000.0)
        probe = [c for c in self.fake.calls if c["probe"]]
        self.assertEqual(probe[0]["region"], (0.0, 60.0, 25.0, 95.0),
                         "探测也要带区域，否则会误判聊天里的同名文字")


class TraeWorkMenuDumpTest(unittest.TestCase):
    r"""用 2026-10-02 的真实菜单清单钉住第二步的关键词与区域。

    清单：logs/ui_dump_traework_menu_20261002_165347.txt（窗口 2561x1529）。
    菜单展开后那一片是：

        [ct50020] 每日领 100 积分           @(47,980)    只是标签文字
        [ct50020] 升级会员，每日多领 100 积分 @(47,1012)
        [ct50000] 签到                     @(313,988)   ← 真按钮

    踩过的坑：第二步原本配的是 ["每日领", "领取"]。引擎是"先匹配到的先点"，
    而「每日领」命中的左侧标签在树序里排在按钮前面，等于点了个寂寞；
    「领取」更糟 —— 左侧任务卡片里就写着"修复自动领取Token问题"，同样是
    可点类型。所以第二步只能认「签到」。
    """

    NEG = ("已领", "已签", "已完成")

    def _steps(self):
        eng = tc.ClaimEngine(_StubCfg(per_app_click={}),
                             log=lambda m, tag="info": None)
        return eng.click_steps_for({"key": "traework"})

    def test_second_step_targets_the_real_button(self):
        self.assertEqual(self._steps()[-1]["keywords"], ["签到"])

    def test_second_step_is_regioned_to_exclude_chat_column(self):
        """菜单右缘 428px，聊天列从 452px 起 —— 右边界必须收在两者之间。"""
        region = self._steps()[-1]["region_pct"]
        self.assertIsNotNone(region, "第二步也要限区域，挡掉正文里的同名词")
        self.assertLess(region[2], 18.0)

    def test_only_the_button_matches_under_chosen_keywords(self):
        """把实测元素名喂给 classify：只有「签到」是候选。"""
        for name, expect in [("用户0433459512 用户0433459512 免费", "skip"),
                             ("3,539", "skip"),
                             ("每日领 100 积分", "skip"),
                             ("升级会员，每日多领 100 积分", "skip"),
                             ("管理账户", "skip"),
                             ("签到", "candidate")]:
            with self.subTest(name=name):
                self.assertEqual(
                    tc.uia_click.classify(name, 50000, ["签到"], self.NEG),
                    expect)

    def test_the_rejected_keywords_really_would_hit_wrong_elements(self):
        """反证：这就是不能配「每日领」「领取」的原因。"""
        task_card = "harness New task 修复自动领取Token问题 Markdown转换项目Bug检测"
        label = "每日领 100 积分"
        self.assertEqual(
            tc.uia_click.classify(label, 50020, ["每日领"], self.NEG),
            "candidate", "「每日领」会命中左侧标签（文本节点）")
        self.assertEqual(
            tc.uia_click.classify(task_card, 50000, ["领取"], self.NEG),
            "candidate", "「领取」会命中左侧任务卡片")


class WorkBuddyMenuDumpTest(unittest.TestCase):
    r"""用 2026-10-03 的真实清单钉住 WorkBuddy 的三步关键词与区域。

    清单：logs/ui_dump_workbuddy_avatar_*.txt（点开头像后的账户菜单）、
          logs/ui_dump_workbuddy_card_*.txt（点开「Buddy加油站」后的卡片）、
          logs/ui_dump_workbuddy_after_*.txt（领取后按钮变「今日已领」）。
    窗口 2561x1529。菜单/卡片里那一片是：

        [ct50020] Hush                               @(437,1261)  ← 头像，可点
        [ct50000] 积分余额 刷新 4,348                 @(393,785)
        [ct50000] Buddy加油站 去邀约 最高得 650 积分/人 @(393,829)  ← 入口
        [ct50000] 设置 Ctrl+,                        @(393,974)
        [ct50000] 退出登录                            @(393,1190)
        --- 卡片展开后 ---
        [ct50000] 关闭 Buddy 加油站                   @(597,1021) 16x16
        [ct50020] 已领 / 累计领取                     @(424,1134)
        [ct50020] Buddy加油站·10期                    @(399,1167)  ← 期数标签
        [ct50000] 立即领取                            @(399,1195)  ← 真按钮
        [ct50000] 今日已领（领取后出现，[禁用]）

    踩过的坑：原来 WorkBuddy 走通用关键词 ["领取","签到","加油站","免费"] 的单步。
    卡片展开后树里第一个含「加油站」的元素是「关闭 Buddy 加油站」，排在真按钮
    前面 —— 引擎"先匹配到的先点"，于是每次都是把卡片点开又点关，用户看到的就是
    "只把加油站点开了，并没有点领取按键"。

    改成多步后第二步的关键词也不能只写「Buddy加油站」：卡片里的期数标签
    「Buddy加油站·10期」同样含这四个字，会被误当成菜单项。所以第二步认的是
    菜单项的完整前缀「Buddy加油站 去邀约」——中间是空格，期数标签那里是「·」。
    """

    NEG = ("已领", "已签", "已完成")
    AVATAR, MENU_ITEM, CLAIM = "Hush", "Buddy加油站 去邀约 最高得 650 积分/人", "立即领取"

    def _steps(self):
        eng = tc.ClaimEngine(_StubCfg(per_app_click={}),
                             log=lambda m, tag="info": None)
        return eng.click_steps_for({"key": "workbuddy"})

    def test_three_steps_in_the_right_order(self):
        steps = self._steps()
        self.assertEqual([s["desc"] for s in steps],
                         ["打开左下角账户菜单", "点击 Buddy 加油站", "点击立即领取"])

    def test_every_step_is_regioned(self):
        """菜单和卡片都叠在左侧栏上方，不限区域就可能点到栏里的同名文字。"""
        for s in self._steps():
            with self.subTest(desc=s["desc"]):
                self.assertIsNotNone(s["region_pct"])

    # 窗口 2561x1529（实测），矩形取自上面的清单
    WIN = types.SimpleNamespace(left=0, top=0, right=2561, bottom=1529)

    @staticmethod
    def _rect(x, y, w, h):
        return types.SimpleNamespace(left=x, top=y, right=x + w, bottom=y + h)

    def test_each_step_region_contains_its_real_target(self):
        """区域必须圈得住真目标 —— 收得太紧把目标也挡在外面就白配了。"""
        steps = self._steps()
        targets = [
            (steps[0], self._rect(437, 1261, 30, 14), "底部头像 Hush"),
            (steps[1], self._rect(393, 829, 307, 88), "菜单项 Buddy加油站"),
            (steps[2], self._rect(399, 1195, 101, 29), "立即领取"),
        ]
        for step, rect, label in targets:
            with self.subTest(target=label):
                self.assertTrue(
                    tc.uia_click.in_region(rect, self.WIN, step["region_pct"]),
                    f"{step['desc']} 的区域圈不到 {label}")

    def test_avatar_region_excludes_the_popup_hush(self):
        """左下角区域只圈底部头像；账户菜单里那个同名 Hush 在区域外。"""
        self.assertFalse(tc.uia_click.in_region(
            self._rect(403, 689, 37, 22), self.WIN,
            self._steps()[0]["region_pct"]))

    def test_second_step_keywords_do_not_match_the_close_button(self):
        """这是本次的核心缺陷：「关闭 Buddy 加油站」不能被第二步命中。"""
        kws = self._steps()[1]["keywords"]
        self.assertNotIn("加油站", kws, "只写「加油站」会命中关闭按钮")
        self.assertEqual(
            tc.uia_click.classify("关闭 Buddy 加油站", 50000, kws, self.NEG),
            "skip")

    def test_only_the_real_targets_match_under_chosen_keywords(self):
        steps = self._steps()
        cases = [
            (self.AVATAR, 50020, steps[0]["keywords"], "candidate"),
            ("Hush Hush", 50011, steps[0]["keywords"], "skip"),   # 容器不可点
            ("积分余额 刷新 4,348", 50000, steps[0]["keywords"], "skip"),
            ("设置 Ctrl+,", 50000, steps[0]["keywords"], "skip"),
            ("退出登录", 50000, steps[0]["keywords"], "skip"),
            (self.MENU_ITEM, 50000, steps[1]["keywords"], "candidate"),
            ("Buddy加油站·10期", 50020, steps[1]["keywords"], "skip"),
            (self.CLAIM, 50000, steps[2]["keywords"], "candidate"),
            ("累计领取", 50020, steps[2]["keywords"], "skip"),
            ("今日已领", 50000, steps[2]["keywords"], "negative"),
        ]
        for name, ctype, kws, expect in cases:
            with self.subTest(name=name):
                self.assertEqual(tc.uia_click.classify(name, ctype, kws, self.NEG),
                                 expect)

    def test_old_global_keywords_really_would_click_the_close_button(self):
        """反证：这就是改成多步、并且第二步关键词收紧的原因。"""
        self.assertEqual(
            tc.uia_click.classify("关闭 Buddy 加油站", 50000,
                                  list(tc.CLICK_KEYWORDS_DEFAULT), self.NEG),
            "candidate")


class SemiAutoHintTest(_EngineCase):
    r"""半自动应用的补充提示：点中最后一步后，把「需要人工做什么」写进日志。

    ZCode 的领取入口点开后弹滑块验证，工具点不了滑块 —— 只能提示用户。这个
    提示挂在 per_app_click 的 hint 字段上，随 job 一路带到 process_click_job，
    在最后一步点击成功的那一刻写出来。
    """

    def _hint_cfg(self):
        self.cfg.data["per_app_click"] = {"zcode": {
            "keywords": ["打开"], "region_pct": [0, 80, 20, 100],
            "hint": "手动过滑块"}}

    def test_hint_is_carried_into_the_job(self):
        self._hint_cfg()
        job = self.eng.make_click_job(
            {"key": "zcode", "label": "ZCode", "exe": r"C:\x\Z.exe"},
            will_launch=False, start_ts=1000.0)
        self.assertEqual(job["hint"], "手动过滑块")

    def test_hint_logged_after_last_step_click(self):
        self._hint_cfg()
        self._install([("clicked", "“打开”")])
        job = self.eng.make_click_job(
            {"key": "zcode", "label": "ZCode", "exe": r"C:\x\Z.exe"},
            will_launch=False, start_ts=1000.0)
        job["deadline"] = NEVER_EXPIRE
        self.eng.process_click_job(job, 2000.0)
        self.assertIn("手动过滑块", self._joined())

    def test_hint_not_logged_when_nothing_clicked(self):
        """没点中就不该提示"去过滑块" —— 那会误导用户以为入口已打开。"""
        self._hint_cfg()
        self._install([("no-element", "未找到匹配按钮")] * 20)
        job = self.eng.make_click_job(
            {"key": "zcode", "label": "ZCode", "exe": r"C:\x\Z.exe"},
            will_launch=False, start_ts=1000.0)
        job["deadline"] = NEVER_EXPIRE
        self.eng.process_click_job(job, 2000.0)
        self.assertNotIn("手动过滑块", self._joined())

    def test_plain_app_has_no_hint(self):
        self.cfg.data["per_app_click"] = {}
        job = self.eng.make_click_job(
            {"key": "trae", "label": "Trae CN", "exe": r"C:\x\Z.exe"},
            will_launch=False, start_ts=1000.0)
        self.assertEqual(job["hint"], "")


class ZCodeBannerDumpTest(unittest.TestCase):
    r"""用 2026-10-03 的真实清单钉住 ZCode 单步入口的关键词与区域。

    清单：logs/ui_dump_zcode_home.txt（窗口 1707x1019）。左下角侧边栏底部：

        [ct50000] 打开  @(16,865) 236x96   ← 领取 banner，中心 (7.9%, 89.6%)
        [ct50000] 关闭  @(222,875) 20x20   ← 旁边的关闭按钮
        [ct50020] 旅行者8156 @(16,971)      ← 账号头像

    同时窗口中部还有一段提示文本也含「打开」：

        [ct50006] 要获取缺失的图片说明，请打开上下文菜单。 @(703,350) 301x201

    关键词「打开」太通用，必须靠左下角区域把提示文本挡在外面 —— 这就是为什么
    ZCode 的预置步骤带 region_pct，而不是直接吃通用关键词。
    """

    NEG = ("已领", "已签", "已完成")
    WIN = types.SimpleNamespace(left=0, top=0, right=1707, bottom=1019)

    @staticmethod
    def _rect(x, y, w, h):
        return types.SimpleNamespace(left=x, top=y, right=x + w, bottom=y + h)

    def _step(self):
        eng = tc.ClaimEngine(_StubCfg(per_app_click={}),
                             log=lambda m, tag="info": None)
        return eng.click_steps_for({"key": "zcode"})[0]

    def test_single_step_targets_the_banner(self):
        self.assertEqual(self._step()["keywords"], ["打开"])

    def test_step_is_regioned(self):
        self.assertIsNotNone(self._step()["region_pct"],
                             "「打开」太通用，必须限区域")

    def test_region_contains_the_banner(self):
        self.assertTrue(tc.uia_click.in_region(
            self._rect(16, 865, 236, 96), self.WIN,
            self._step()["region_pct"]))

    def test_region_excludes_the_toolbar_tooltip(self):
        """含「打开」的提示文本在中部，必须落在区域外。"""
        self.assertFalse(tc.uia_click.in_region(
            self._rect(703, 350, 301, 201), self.WIN,
            self._step()["region_pct"]))

    def test_region_does_not_hit_the_close_button(self):
        """旁边的「关闭」不能被「打开」命中。"""
        self.assertEqual(
            tc.uia_click.classify("关闭", 50000, ["打开"], self.NEG), "skip")

    def test_tooltip_text_would_match_without_the_region(self):
        """反证：不配区域时那段提示文本（可点类型）会被误当成候选。"""
        self.assertEqual(
            tc.uia_click.classify("要获取缺失的图片说明，请打开上下文菜单。",
                                  50000, ["打开"], self.NEG),
            "candidate")

    # 2026-10-03 09:26 的实时清单（窗口 2561x1529，logs/ui_dump_zcode_live_0926.txt）：
    # 当天领取已完成，banner 已消失，整棵树里只剩右上角工具栏两个含「打开」的按钮。
    BIG_WIN = types.SimpleNamespace(left=0, top=0, right=2561, bottom=1529)
    TOOLBAR_OPEN = "在 资源管理器 中打开"

    def test_toolbar_open_button_is_excluded_by_the_region(self):
        """右上角工具栏的「在 资源管理器 中打开」@(2184,22) 必须落在区域外。"""
        self.assertEqual(
            tc.uia_click.classify(self.TOOLBAR_OPEN, 50000, ["打开"], self.NEG),
            "candidate", "没有区域时它会被当成候选 —— 所以区域是必须的")
        self.assertFalse(tc.uia_click.in_region(
            self._rect(2184, 22, 42, 42), self.BIG_WIN,
            self._step()["region_pct"]))

    def test_region_still_covers_the_banner_slot_on_a_bigger_window(self):
        """窗口放大到 2561x1529 时，banner 槽位（账号行上方）仍落在区域内。

        账号行实测 @(24,1457) 237x48；banner 在它上方、高约 96px，推算中心
        约 (5.5%, 91.5%)。区域按百分比定义，窗口变大也不会失效。
        """
        self.assertTrue(tc.uia_click.in_region(
            self._rect(24, 1351, 237, 96), self.BIG_WIN,
            self._step()["region_pct"]))

    def test_semi_auto_hint_is_present(self):
        self.assertIn("zcode", tc.DEFAULT_PER_APP_CLICK)
        self.assertTrue(tc.DEFAULT_PER_APP_CLICK["zcode"].get("hint"),
                        "ZCode 需人工过滑块，必须带 hint 提示")


class MinimizedWindowGuardTest(unittest.TestCase):
    r"""窗口最小化时区域过滤不能静默失效。

    背景：窗口最小化后 GetWindowRect 返回垃圾值（实测 ZCode 拿到
    (-21333,-21333,-21175,-21307)，尺寸 158x26），拿它当窗口矩形做百分比
    区域过滤，会把本来在区域内的元素判成"不在区域内"，最终报成 no-element
    —— 看起来像"按钮没找到"，实则窗口矩形本身是坏的。修法是用 UIA 根元素的
    包围盒，并在矩形不可用时返回明确的 minimized 状态。
    """

    GARBAGE = types.SimpleNamespace(left=-21333, top=-21333,
                                    right=-21175, bottom=-21307)

    def test_garbage_rect_is_rejected(self):
        self.assertFalse(tc.uia_click._rect_usable(self.GARBAGE))

    def test_normal_rect_is_accepted(self):
        self.assertTrue(tc.uia_click._rect_usable(
            types.SimpleNamespace(left=0, top=0, right=1707, bottom=1019)))

    def test_degenerate_rect_is_rejected(self):
        self.assertFalse(tc.uia_click._rect_usable(
            types.SimpleNamespace(left=10, top=10, right=10, bottom=10)))

    def test_none_rect_is_rejected(self):
        self.assertFalse(tc.uia_click._rect_usable(None))

    def test_offscreen_left_monitor_window_is_accepted(self):
        """副屏在主屏左侧时窗口坐标是负数，不该被当成最小化垃圾矩形。"""
        from unittest import mock
        with mock.patch.object(tc.uia_click, "_virtual_screen",
                               return_value=(-1920, 0, 3840, 1080)):
            self.assertTrue(tc.uia_click._rect_usable(
                types.SimpleNamespace(left=-1920, top=0, right=0, bottom=1080)))

    def test_garbage_rect_rejected_against_normal_screen(self):
        from unittest import mock
        with mock.patch.object(tc.uia_click, "_virtual_screen",
                               return_value=(0, 0, 1920, 1080)):
            self.assertFalse(tc.uia_click._rect_usable(self.GARBAGE))

    def test_in_region_returns_false_for_garbage_window_rect(self):
        """垃圾窗口矩形下 in_region 判 False（而不是错判为 True）。"""
        self.assertFalse(tc.uia_click.in_region(
            types.SimpleNamespace(left=16, top=865, right=252, bottom=961),
            self.GARBAGE, (0, 80, 20, 100)))


class ClickPacingTest(_EngineCase):
    r"""点击节奏：不再死等"窗口等待"，失败后按退避重试。

    背景（2026-10-04，用户反馈"领取有点慢"）：实测一次 WorkBuddy 领取共 33 秒，
    其中 27.9 秒是启动后固定死等 click_wait_window（默认 25 秒）才第一次点击；
    失败后的重试也固定 15 秒。而界面树实测约 5 秒就长出来，死等纯属浪费。
    """

    def test_launched_job_starts_soon_instead_of_waiting_the_whole_window(self):
        job = self.eng.make_click_job(APP, will_launch=True, start_ts=1000.0)
        self.assertEqual(job["next"], 1000.0 + tc.START_DELAY)
        self.assertLess(job["next"] - 1000.0, job["wait"],
                        "启动后应很快开始尝试，而不是死等整个窗口等待")

    def test_retry_delay_backs_off_and_caps_at_configured_retry(self):
        job = self._job()
        job["attempts"] = 1
        self.assertAlmostEqual(self.eng._retry_delay(job), job["fast_retry"])
        job["attempts"] = 2
        self.assertGreater(self.eng._retry_delay(job), job["fast_retry"],
                           "重试间隔要递增，先抓住就绪瞬间，再放慢节奏")
        job["attempts"] = 50
        self.assertAlmostEqual(self.eng._retry_delay(job), job["retry"],
                               msg="退避要封顶在配置的重试间隔")

    def test_step_transition_wait_is_short(self):
        steps = self.eng.click_steps_for({"key": "workbuddy"})
        self.assertTrue(all(s["wait"] <= 1.5 for s in steps),
                        "多步之间的等待要短，没渲染好由退避重试接住")

    def test_waiting_for_the_tree_does_not_burn_the_attempt_budget(self):
        """启动初期树还在长，这段等待不该把重试预算用光（否则自愈还没到点就放弃）。"""
        self.cfg.data["click_max_attempts"] = 3
        self._install([("no-tree", "界面树没有内容")] * 20)
        job = self._job()          # will_launch=False → no_tree_wait=20 秒
        for i in range(10):
            self.eng.process_click_job(job, 2000.0 + i)
        self.assertFalse(job["done"], "还在等树的窗口内，不该放弃")
        self.assertLess(job["attempts"], job["max"])


class ForegroundActivationTest(unittest.TestCase):
    r"""查找元素前必须先把目标窗口激活到前台。

    背景（2026-10-04 线上故障）：WorkBuddy 的 UIA 树只在窗口处于前台时才存在，
    被别的窗口抢走前台后几秒，树就从 ~180 塌成 8 个无名 Pane，此后所有关键词
    查找都落空（日志里表现为"界面树始终为空"）。实测重新激活窗口后树 2 秒内
    恢复，所以 find_and_click 必须先置前，否则必然 no-tree。

    这里把 Win32 部分换成假实现，只钉住"查找前调用了 _bring_to_foreground"。
    """

    def setUp(self):
        self.uc = tc.uia_click
        self.orig = {n: getattr(self.uc, n) for n in
                     ("available", "list_pids", "_visible_windows",
                      "_bring_to_foreground", "_uia")}
        self.calls = []

        class _FakeUia:
            def ElementFromHandle(self, hwnd):
                raise RuntimeError("到此为止，后面不需要真实枚举")

        self.uc.available = lambda: True
        self.uc._uia = _FakeUia()
        self.uc.list_pids = lambda image: [111]
        self.uc._visible_windows = lambda pids: [222]
        self.uc._bring_to_foreground = (
            lambda hwnd: self.calls.append(hwnd) or False)
        self.addCleanup(self._restore)

    def _restore(self):
        for name, val in self.orig.items():
            setattr(self.uc, name, val)

    def test_window_is_activated_before_lookup(self):
        self.uc.find_and_click("X.exe", ["领取"])
        self.assertEqual(self.calls, [222], "查找前必须先激活目标窗口")

    def test_probe_also_activates_the_window(self):
        """探测（跳步判断）同样依赖健康的树，也要先置前。"""
        self.uc.find_and_click("X.exe", ["领取"], probe=True)
        self.assertEqual(self.calls, [222])

    def test_warmup_constants_are_sane(self):
        self.assertGreater(self.uc.TREE_WARMUP_MAX, 0,
                           "置前后要留时间让树重建，不能为 0")
        self.assertGreater(self.uc.TREE_READY_MIN, 8,
                           "就绪阈值必须高于塌陷态的 8 个元素")


if __name__ == "__main__":
    unittest.main()
