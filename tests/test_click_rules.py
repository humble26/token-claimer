# -*- coding: utf-8 -*-
r"""智能点击「点哪里」的判定规则 + 按应用关键词覆盖的回归测试。

背景（对着 TraeWork CN 的真实界面核对出来的两个缺陷）：
  1. 领取入口那行「每日领 100 积分」只是左侧标签（文本节点，而且在树序里排在
     按钮前面），点它等于白点；同一行右侧的真按钮叫「签到」。通用关键词里的
     「领取」会命中聊天正文和左侧任务卡片，所以按应用覆盖成「签到」
     （见 DEFAULT_PER_APP_CLICK["traework"]）。
  2. 该按钮已领取时显示「今日已签」，而负向词原是「已签到」，多一个「到」字，
     匹配不到 —— 于是既点不到、也认不出"今天已完成"。
另外同一菜单里还有「立即升级」「管理账户」「免费」等控件，必须确保不被误点。

运行：python -m unittest discover -s tests
"""

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# token_claimer.py 模块级 import tkinter，测试环境先注入桩再导入。
for name in ("tkinter", "tkinter.filedialog", "tkinter.messagebox", "tkinter.ttk"):
    if name not in sys.modules:
        mod = types.ModuleType(name)
        if name == "tkinter":
            for attr in ("Tk", "Toplevel", "StringVar", "BooleanVar", "IntVar"):
                setattr(mod, attr, object)
        sys.modules[name] = mod

import token_claimer as tc  # noqa: E402
import uia_click as uc  # noqa: E402

CT_BUTTON = 50000          # 在 CLICKABLE_TYPES 里
CT_DOCUMENT = 50034        # 不在 CLICKABLE_TYPES 里
NEG = ("已领", "已签", "已完成")
# TraeWork CN 是两步入口，这里取「点领取」那一步的关键词做分类判定
TRAEWORK_KW = tc.DEFAULT_PER_APP_CLICK["traework"]["steps"][-1]["keywords"]


class ClassifyTest(unittest.TestCase):
    """判定规则本身：谁能点、谁算已完成、谁必须放过。"""

    def test_already_signed_is_negative(self):
        """「今日已签」必须被认成已完成 —— 原缺陷用「已签到」匹配不到。"""
        self.assertEqual(uc.classify("今日已签", CT_BUTTON, TRAEWORK_KW, NEG),
                         "negative")

    def test_legacy_negative_words_still_work(self):
        for nm in ("今日已领", "已签到", "已完成"):
            with self.subTest(name=nm):
                self.assertEqual(uc.classify(nm, CT_BUTTON, TRAEWORK_KW, NEG),
                                 "negative")

    def test_default_negative_tuple_covers_yi_qian(self):
        """默认参数也必须覆盖「已签」，否则 process_click_job 调用时又漏掉。"""
        import inspect
        default = inspect.signature(uc.find_and_click).parameters["negative"].default
        self.assertIn("已签", default)

    def test_daily_claim_row_is_not_the_target(self):
        """「每日领 100 积分」只是左侧标签，不该被当成领取按钮。"""
        self.assertEqual(
            uc.classify("每日领 100 积分", CT_BUTTON, TRAEWORK_KW, NEG),
            "skip")

    def test_sign_in_button_is_candidate(self):
        """真正的领取按钮叫「签到」，按应用覆盖后必须命中它。"""
        self.assertEqual(uc.classify("签到", CT_BUTTON, TRAEWORK_KW, NEG),
                         "candidate")

    def test_unrelated_menu_items_are_skipped(self):
        """同一账户菜单里的其它控件不能被误点，尤其是「立即升级」。"""
        for nm in ("立即升级", "¥39 升级权益", "管理账户", "消息", "设置",
                   "报告问题", "退出登录", "简体中文", "亮色"):
            with self.subTest(name=nm):
                self.assertEqual(uc.classify(nm, CT_BUTTON, TRAEWORK_KW, NEG),
                                 "skip")

    def test_free_badge_is_skipped_under_app_specific_keywords(self):
        """用户名旁的「免费」标签：通用词会命中，按应用覆盖后必须放过。"""
        self.assertEqual(uc.classify("免费", CT_BUTTON, TRAEWORK_KW, NEG), "skip")

    def test_global_keywords_miss_the_daily_row(self):
        """对照实验：证明通用关键词确实命中不了，必须靠 per-app 覆盖。"""
        self.assertEqual(
            uc.classify("每日领 100 积分", CT_BUTTON,
                        tc.CLICK_KEYWORDS_DEFAULT, NEG),
            "skip")

    def test_non_clickable_control_type_is_skipped(self):
        """文字节点即便名字匹配也不能点，否则会点到不可交互的文本上。"""
        self.assertEqual(
            uc.classify("每日领 100 积分", CT_DOCUMENT, TRAEWORK_KW, NEG),
            "skip")

    def test_negative_wins_over_keyword(self):
        self.assertEqual(uc.classify("每日领已签", CT_BUTTON, TRAEWORK_KW, NEG),
                         "negative")


class ElementVerdictTest(unittest.TestCase):
    r"""遍历元素时的最终判定：完成标识必须先于"跳过禁用元素"。

    实测 TraeWork CN 领取后按钮变成 [禁用] 的「今日已签」（CurrentIsEnabled
    = False），WorkBuddy 是 [禁用] 的「今日已领」。classify 本身判得对，但遍历
    里先按 enabled 过滤再判定，于是这些完成标识被当成"没找到按钮"，一路重试到
    预算耗尽 —— 实测白转 78 秒。顺序在这里钉死，免得又被改回去。
    """

    def test_disabled_already_claimed_button_is_still_negative(self):
        self.assertEqual(
            uc.element_verdict("今日已签", CT_BUTTON, False, False, True,
                               TRAEWORK_KW, NEG),
            "negative",
            "已领取的按钮是 [禁用] 的，不能因为禁用就丢掉完成标识")

    def test_offscreen_negative_is_ignored(self):
        """聊天正文里的「已完成」不能当成今天已领。"""
        self.assertEqual(
            uc.element_verdict("已完成 修复自动领取Token问题", CT_BUTTON,
                               True, True, True, TRAEWORK_KW, NEG),
            "skip")

    def test_negative_outside_region_is_ignored(self):
        self.assertEqual(
            uc.element_verdict("今日已签", CT_BUTTON, False, True, False,
                               TRAEWORK_KW, NEG),
            "skip")

    def test_disabled_candidate_is_not_clickable(self):
        self.assertEqual(
            uc.element_verdict("签到", CT_BUTTON, False, False, True,
                               TRAEWORK_KW, NEG),
            "skip")

    def test_enabled_in_region_candidate_matches(self):
        self.assertEqual(
            uc.element_verdict("签到", CT_BUTTON, False, True, True,
                               TRAEWORK_KW, NEG),
            "candidate")


class _StubCfg:
    """不落盘的配置，避免测试读写用户真实的 config.json。"""

    def __init__(self, **overrides):
        self.data = {
            "click_enabled": False,
            "click_keywords": list(tc.CLICK_KEYWORDS_DEFAULT),
            "per_app_click": {},
        }
        self.data.update(overrides)

    def save(self):
        pass


class PerAppClickCfgTest(unittest.TestCase):
    """预置覆盖与用户覆盖的优先级。"""

    def test_builtin_override_applies_to_old_config(self):
        """老配置的 per_app_click 是空的，也要能拿到预置的两步序列。"""
        eng = tc.ClaimEngine(_StubCfg())
        steps = eng.click_steps_for({"key": "traework"})
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[-1]["keywords"], TRAEWORK_KW)
        self.assertIsNone(steps[-1]["point"])

    def test_user_keywords_replace_builtin_steps(self):
        """用户改用单步写法时，预置的 steps 必须整体让位，不能混在一起。"""
        eng = tc.ClaimEngine(_StubCfg(
            per_app_click={"traework": {"keywords": ["自定义词"]}}))
        steps = eng.click_steps_for({"key": "traework"})
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["keywords"], ["自定义词"])

    def test_user_point_replaces_builtin_steps(self):
        eng = tc.ClaimEngine(_StubCfg(
            per_app_click={"traework": {"point": [16.0, 97.0]}}))
        steps = eng.click_steps_for({"key": "traework"})
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["point"], (16.0, 97.0))
        self.assertEqual(steps[0]["keywords"], list(tc.CLICK_KEYWORDS_DEFAULT))

    def test_user_steps_win_wholesale(self):
        eng = tc.ClaimEngine(_StubCfg(per_app_click={
            "traework": {"steps": [{"keywords": ["甲"]}, {"keywords": ["乙"]},
                                   {"keywords": ["丙"]}]}}))
        steps = eng.click_steps_for({"key": "traework"})
        self.assertEqual([s["keywords"] for s in steps],
                         [["甲"], ["乙"], ["丙"]])

    def test_other_app_falls_back_to_global_keywords(self):
        """没有预置覆盖的应用（如 Trae CN）走通用关键词，仍是单步。"""
        eng = tc.ClaimEngine(_StubCfg())
        steps = eng.click_steps_for({"key": "trae"})
        self.assertEqual(len(steps), 1)            # 单步应用就是一个步骤
        self.assertEqual(steps[0]["keywords"],
                         list(tc.CLICK_KEYWORDS_DEFAULT))
        self.assertIsNone(steps[0]["point"])

    def test_launch_args_override_still_honoured(self):
        eng = tc.ClaimEngine(_StubCfg(
            per_app_click={"traework": {"launch_args": ["--foo"]}}))
        self.assertEqual(eng.launch_extra_args({"key": "traework"}), ["--foo"])

    def test_launch_args_do_not_disturb_builtin_steps(self):
        """launch_args 不是"点哪里"的键，不该把预置步骤挤掉。"""
        eng = tc.ClaimEngine(_StubCfg(
            per_app_click={"traework": {"launch_args": ["--foo"]}}))
        self.assertEqual(len(eng.click_steps_for({"key": "traework"})), 2)


class FormatElementTest(unittest.TestCase):
    """校准清单里每一行的格式：位置、状态、以及能不能被点到。"""

    RECT = types.SimpleNamespace(left=10, top=20, right=210, bottom=60)

    def _line(self, ctype=CT_BUTTON, enabled=True, offscreen=0, rect=None):
        return uc.format_element("某元素", ctype, rect, enabled, offscreen)

    def test_clickable_type_is_marked(self):
        self.assertIn("[可点]", self._line(ctype=CT_BUTTON))

    def test_non_clickable_type_is_not_marked(self):
        """校准时要避开这类元素 —— 名字对得上也点不动。"""
        self.assertNotIn("[可点]", self._line(ctype=CT_DOCUMENT))

    def test_position_is_window_pixels(self):
        self.assertIn("@(10,20) 200x40", self._line(rect=self.RECT))

    def test_degenerate_rect_omits_position(self):
        self.assertNotIn("@", self._line(rect=types.SimpleNamespace(
            left=10, top=20, right=10, bottom=20)))

    def test_missing_rect_omits_position(self):
        self.assertNotIn("@", self._line(rect=None))

    def test_disabled_and_offscreen_flags(self):
        line = self._line(enabled=False, offscreen=1)
        self.assertIn("[禁用]", line)
        self.assertIn("[屏幕外]", line)

    def test_enabled_and_onscreen_have_no_flags(self):
        line = self._line(enabled=True, offscreen=0)
        self.assertNotIn("[禁用]", line)
        self.assertNotIn("[屏幕外]", line)

    def test_long_name_is_truncated(self):
        line = uc.format_element("长" * 200, CT_BUTTON, None, True, 0)
        self.assertLessEqual(len(line), 70 + len("[ct50000] ") + len(" [可点]"))


class AvailableCacheTest(unittest.TestCase):
    """comtypes 探测结果的缓存必须是三态（None/False/模块），不能把 False 当可用。

    原缺陷：available() 写成 `if _uia_module is not None: return True`。首次探测
    失败后缓存成 False，第二次调用却因为"不是 None"而返回 True —— 在没装
    comtypes 的机器上，同一次领取里 run_claim 先问一次得到"不可用"，紧接着
    launch_extra_args 再问一次就变成"可用"，于是给客户端多加了无障碍参数，
    还生成了永远点不中的点击任务。
    """

    def setUp(self):
        self._orig = uc._uia_module
        self.addCleanup(lambda: setattr(uc, "_uia_module", self._orig))

    def test_failed_probe_stays_false_on_second_call(self):
        uc._uia_module = False
        self.assertFalse(uc.available())
        self.assertFalse(uc.available(), "第二次调用不能翻成 True")

    def test_successful_probe_reports_true(self):
        uc._uia_module = object()
        self.assertTrue(uc.available())


if __name__ == "__main__":
    unittest.main()
