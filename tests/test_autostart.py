# -*- coding: utf-8 -*-
r"""自启动 .vbs 生成的回归测试。

背景（均为已修复的真实缺陷）：原实现用 json.dumps 生成 VBScript 字符串字面量，
同时踩了四个坑：
  1. json.dumps 把 " 转义成 \"，而 VBScript 唯一的引号转义是双写 ""，
     于是 \" 被解析成「反斜杠 + 字符串结束」→ 整个脚本语法非法；
  2. json.dumps 把每个 \ 翻倍成 \\，即使引号通过，路径也指向不存在的位置；
  3. json.dumps 默认 ensure_ascii=True，把中文目录名转义成 \u9886\u53d6... 字面量；
  4. 文件按 utf-8 写，而 WSH 默认按 ANSI 读，中文路径乱码。
任何一个未修，自启都会静默失效 —— 用户以为开了，实际没跑。

运行：python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

# token_claimer.py 在仓库根目录，且模块级 import tkinter；
# 测试环境可能没有 tkinter，先注入桩再导入。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import types

for name in ("tkinter", "tkinter.filedialog", "tkinter.messagebox", "tkinter.ttk"):
    if name not in sys.modules:
        mod = types.ModuleType(name)
        if name == "tkinter":
            mod.Tk = object
            mod.Toplevel = object
            mod.StringVar = object
            mod.BooleanVar = object
            mod.IntVar = object
        sys.modules[name] = mod

import token_claimer as tc  # noqa: E402


# ---- 独立的 VBScript 字符串字面量解析器 -------------------------------------
# 不依赖 wscript（沙箱禁止调用它），用语言规范本身来交叉验证生成的文本是否合法。
def parse_vbs_string_literal(s: str, i: int = 0):
    """从 s[i] 起解析一个 VBScript 字符串字面量，返回 (解析出的值, 结束位置)。

    文法：以 " 开始，内部 "" 表示一个真实引号，遇到单个 " 结束。
    """
    if i >= len(s) or s[i] != '"':
        raise ValueError(f"位置 {i} 不是字符串起始引号")
    i += 1
    out = []
    while i < len(s):
        if s[i] == '"':
            if i + 1 < len(s) and s[i + 1] == '"':
                out.append('"')
                i += 2
                continue
            return "".join(out), i + 1
        out.append(s[i])
        i += 1
    raise ValueError("字符串字面量未闭合（这正是 \\\" 转义会造成的故障）")


def extract_run_target(vbs_text: str) -> str:
    """从 ws.Run "...", 0, False 里取出那个字符串字面量并解析。"""
    line = next(l for l in vbs_text.splitlines() if l.startswith("ws.Run "))
    start = line.index('"')
    value, end = parse_vbs_string_literal(line, start)
    rest = line[end:]
    assert rest.startswith(", 0, False"), f"ws.Run 参数结构异常: {rest!r}"
    return value


# ---------------------------------------------------------------------------
class TestVbsLiteral(unittest.TestCase):
    def test_quotes_are_doubled_not_backslash_escaped(self):
        self.assertEqual(tc._vbs_literal('a"b'), '"a""b"')
        self.assertNotIn('\\"', tc._vbs_literal('a"b'), "不得出现反斜杠转义（坑 1）")

    def test_backslashes_are_not_doubled(self):
        lit = tc._vbs_literal(r"C:\Users\me\x.py")
        self.assertEqual(lit, '"C:\\Users\\me\\x.py"'.replace("\\\\", "\\"),
                         "反斜杠保持原样（坑 2）")
        self.assertNotIn("\\\\", lit, "反斜杠不得翻倍")

    def test_non_ascii_is_not_escaped(self):
        lit = tc._vbs_literal(r"E:\harness\12-Token领取助手\token_claimer.py")
        self.assertIn("领取助手", lit, "中文必须原样保留（坑 3）")
        self.assertNotIn("\\u", lit, "不得出现 \\uXXXX 转义")

    def test_literal_round_trips_through_vbscript_grammar(self):
        for raw in [
            r'C:\plain\path.py',
            r'E:\harness\12-Token领取助手\token_claimer.py',
            r'C:\Program Files (x86)\My App\a.exe',
            'name with "quotes" and 中文',
        ]:
            lit = tc._vbs_literal(raw)
            value, end = parse_vbs_string_literal(lit, 0)
            self.assertEqual(value, raw, f"往返不一致: {raw!r}")
            self.assertEqual(end, len(lit), "字面量后不应有多余字符")


class TestAutostartVbsText(unittest.TestCase):
    def test_run_line_is_valid_vbscript_and_target_matches(self):
        target = r'"C:\Python\pythonw.exe" "E:\harness\12-Token领取助手\token_claimer.py"'
        text = tc.autostart_vbs_text(target)
        # 用文法解析器独立验证：能解析且往返得到原目标
        self.assertEqual(extract_run_target(text), target)
        self.assertTrue(text.startswith('Set ws = CreateObject("WScript.Shell")'))

    def test_no_json_artifacts_anywhere(self):
        text = tc.autostart_vbs_text(r'"C:\a b\pythonw.exe" "E:\中 文\s.py"')
        self.assertNotIn('\\"', text, "出现 \\\" 即语法非法")
        self.assertNotIn("\\\\", text, "出现 \\\\ 即路径翻倍")
        self.assertNotIn("\\u", text, "出现 \\uXXXX 即中文被转义")

    def test_real_target_uses_pythonw_or_script_path(self):
        # 不冻结时目标是两段独立字面量： "runner" "script"
        t = tc.autostart_target()
        v1, e1 = parse_vbs_string_literal(t, 0)
        self.assertEqual(t[e1], " ", "两个字面量之间应是空格")
        v2, e2 = parse_vbs_string_literal(t, e1 + 1)
        self.assertEqual(e2, len(t), "第二个字面量后不应有多余字符")
        self.assertTrue(v1.endswith(("pythonw.exe", "python.exe")),
                        f"第一段应是解释器: {v1}")
        self.assertEqual(Path(v2).name, "token_claimer.py", f"第二段应是脚本: {v2}")


class TestSetAutostartRoundTrip(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.vbs = Path(self.tmp.name) / "Token领取助手_自启动.vbs"
        self._orig = tc.autostart_vbs_path
        tc.autostart_vbs_path = lambda: self.vbs

    def tearDown(self):
        tc.autostart_vbs_path = self._orig
        self.tmp.cleanup()

    def test_enable_then_disable(self):
        self.assertFalse(tc.autostart_enabled())
        self.assertEqual(tc.set_autostart(True), "已开启开机自启")
        self.assertTrue(self.vbs.exists())
        self.assertEqual(tc.set_autostart(False), "已关闭开机自启")
        self.assertFalse(self.vbs.exists())

    def test_written_file_is_utf16_bom_and_decodes_correctly(self):
        # 自带含中文的目标样本：不能依赖「本仓库路径恰好含中文」——
        # CI 上仓库在 D:\a\… 这类全英文路径下，原写法会误报（首次 CI 运行已发生）。
        fake = r'"C:\Python\pythonw.exe" "D:\测试目录\12-Token领取助手\token_claimer.py"'
        orig = tc.autostart_target
        tc.autostart_target = lambda: fake
        try:
            tc.set_autostart(True)
        finally:
            tc.autostart_target = orig
        raw = self.vbs.read_bytes()
        # 坑 4：必须是 WSH 能识别的 Unicode 编码
        self.assertTrue(raw.startswith(b"\xff\xfe"),
                        "缺少 UTF-16LE BOM —— WSH 会按 ANSI 读，中文路径乱码")
        text = raw.decode("utf-16")
        self.assertIn("领取助手", text, "写入后中文仍应可读")
        self.assertEqual(extract_run_target(text), fake)

    def test_round_trip_preserves_exact_target(self):
        tc.set_autostart(True)
        text = self.vbs.read_bytes().decode("utf-16")
        got = extract_run_target(text)
        self.assertEqual(got, tc.autostart_target())
        # 目标里的两个路径都必须真实存在，否则自启会静默失败
        parts = got.split('" "')
        for p in (parts[0].lstrip('"'), parts[1].rstrip('"')):
            self.assertTrue(Path(p).exists(), f"自启目标路径不存在: {p}")


class TestVbsParserItself(unittest.TestCase):
    """解析器自身的测试 —— 保证上面那些断言不是永远为真的空转。"""

    def test_detects_unterminated_literal(self):
        with self.assertRaises(ValueError, msg="未闭合的字面量必须报错"):
            parse_vbs_string_literal('"abc', 0)

    def test_old_buggy_output_is_truncated_by_vbscript_grammar(self):
        """旧实现用 json.dumps 生成的产物，在 VBS 文法下会提前闭合。"""
        import json
        target = r'"C:\x\pythonw.exe" "C:\y\s.py"'
        old_literal = json.dumps(target)          # 旧实现的产物
        value, end = parse_vbs_string_literal(old_literal, 0)
        self.assertNotEqual(value, target, "旧产物必须无法还原出目标（否则测试无意义）")
        self.assertLess(end, len(old_literal),
                        "旧产物会在第一个 \\\" 处提前闭合，尾部无法解析 → 语法错误")


if __name__ == "__main__":
    unittest.main()
