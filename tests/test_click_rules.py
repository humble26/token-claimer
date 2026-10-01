# -*- coding: utf-8 -*-
r"""智能点击「点哪里」的判定规则 + 按应用关键词覆盖的回归测试。

背景（对着 TraeWork CN 的真实界面核对出来的两个缺陷）：
  1. 领取入口那行叫「每日领 100 积分」，通用关键词（领取/签到/加油站/免费）
     一个都命中不了 —— 含「领」但不含「领取」。
  2. 该行右侧按钮已领取时显示「今日已签」，而负向词是「已签到」，多一个
     「到」字，匹配不到 —— 于是既点不到、也认不出"今天已完成"。
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
TRAE_SOLO_KW = tc.DEFAULT_PER_APP_CLICK["trae_solo"]["keywords"]


class ClassifyTest(unittest.TestCase):
    """判定规则本身：谁能点、谁算已完成、谁必须放过。"""

    def test_already_signed_is_negative(self):
        """「今日已签」必须被认成已完成 —— 原缺陷用「已签到」匹配不到。"""
        self.assertEqual(uc.classify("今日已签", CT_BUTTON, TRAE_SOLO_KW, NEG),
                         "negative")

    def test_legacy_negative_words_still_work(self):
        for nm in ("今日已领", "已签到", "已完成"):
            with self.subTest(name=nm):
                self.assertEqual(uc.classify(nm, CT_BUTTON, TRAE_SOLO_KW, NEG),
                                 "negative")

    def test_default_negative_tuple_covers_yi_qian(self):
        """默认参数也必须覆盖「已签」，否则 process_click_job 调用时又漏掉。"""
        import inspect
        default = inspect.signature(uc.find_and_click).parameters["negative"].default
        self.assertIn("已签", default)

    def test_daily_claim_row_is_candidate(self):
        self.assertEqual(
            uc.classify("每日领 100 积分", CT_BUTTON, TRAE_SOLO_KW, NEG),
            "candidate")

    def test_claim_button_is_candidate(self):
        self.assertEqual(uc.classify("领取", CT_BUTTON, TRAE_SOLO_KW, NEG),
                         "candidate")

    def test_unrelated_menu_items_are_skipped(self):
        """同一账户菜单里的其它控件不能被误点，尤其是「立即升级」。"""
        for nm in ("立即升级", "¥39 升级权益", "管理账户", "消息", "设置",
                   "报告问题", "退出登录", "简体中文", "亮色"):
            with self.subTest(name=nm):
                self.assertEqual(uc.classify(nm, CT_BUTTON, TRAE_SOLO_KW, NEG),
                                 "skip")

    def test_free_badge_is_skipped_under_app_specific_keywords(self):
        """用户名旁的「免费」标签：通用词会命中，按应用覆盖后必须放过。"""
        self.assertEqual(uc.classify("免费", CT_BUTTON, TRAE_SOLO_KW, NEG), "skip")

    def test_global_keywords_miss_the_daily_row(self):
        """对照实验：证明通用关键词确实命中不了，必须靠 per-app 覆盖。"""
        self.assertEqual(
            uc.classify("每日领 100 积分", CT_BUTTON,
                        tc.CLICK_KEYWORDS_DEFAULT, NEG),
            "skip")

    def test_non_clickable_control_type_is_skipped(self):
        """文字节点即便名字匹配也不能点，否则会点到不可交互的文本上。"""
        self.assertEqual(
            uc.classify("每日领 100 积分", CT_DOCUMENT, TRAE_SOLO_KW, NEG),
            "skip")

    def test_negative_wins_over_keyword(self):
        self.assertEqual(uc.classify("每日领已签", CT_BUTTON, TRAE_SOLO_KW, NEG),
                         "negative")


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
        """老配置的 per_app_click 是空的，也要能拿到预置关键词。"""
        eng = tc.ClaimEngine(_StubCfg())
        kw, point = eng.click_settings_for({"key": "trae_solo"})
        self.assertEqual(kw, TRAE_SOLO_KW)
        self.assertIsNone(point)

    def test_user_override_wins(self):
        eng = tc.ClaimEngine(_StubCfg(
            per_app_click={"trae_solo": {"keywords": ["自定义词"]}}))
        kw, _ = eng.click_settings_for({"key": "trae_solo"})
        self.assertEqual(kw, ["自定义词"])

    def test_user_point_override_wins(self):
        eng = tc.ClaimEngine(_StubCfg(
            per_app_click={"trae_solo": {"point": [16.0, 97.0]}}))
        kw, point = eng.click_settings_for({"key": "trae_solo"})
        self.assertEqual(kw, TRAE_SOLO_KW)          # 关键词仍来自预置
        self.assertEqual(point, (16.0, 97.0))       # 坐标来自用户

    def test_other_app_falls_back_to_global_keywords(self):
        eng = tc.ClaimEngine(_StubCfg())
        kw, point = eng.click_settings_for({"key": "workbuddy"})
        self.assertEqual(kw, list(tc.CLICK_KEYWORDS_DEFAULT))
        self.assertIsNone(point)

    def test_launch_args_override_still_honoured(self):
        eng = tc.ClaimEngine(_StubCfg(
            per_app_click={"trae_solo": {"launch_args": ["--foo"]}}))
        self.assertEqual(eng.launch_extra_args({"key": "trae_solo"}), ["--foo"])


if __name__ == "__main__":
    unittest.main()
