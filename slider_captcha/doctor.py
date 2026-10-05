# -*- coding: utf-8 -*-
"""环境自检：报告本解释器下各依赖的可用性，便于宿主安装流程与排障。

用法：python -m slider_captcha.doctor
"""
import importlib
import sys


def _check(name, probe):
    """probe() 返回描述字符串表示可用，抛异常/返回 None 表示缺失。"""
    try:
        desc = probe()
        return True, desc
    except Exception as e:                     # noqa: BLE001 - 自检要吞掉一切错误
        return False, f'{type(e).__name__}: {e}'


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    rows = []
    rows.append(('Python', True, f'{sys.version_info.major}.{sys.version_info.minor}'
                 f'.{sys.version_info.micro} ({sys.executable})'))

    ok, desc = _check('numpy', lambda: importlib.import_module('numpy').__version__)
    rows.append(('numpy（必需）', ok, desc))

    def _cv2():
        cv2 = importlib.import_module('cv2')
        return f'{cv2.__version__} @ {cv2.__file__}'
    ok, desc = _check('cv2', _cv2)
    rows.append(('opencv（图像定位必需）', ok, desc))

    def _pw():
        pw = importlib.import_module('playwright')
        return getattr(pw, '__version__', '已安装')
    ok, desc = _check('playwright', _pw)
    rows.append(('playwright（浏览器求解）', ok, desc))

    def _chromium():
        with importlib.import_module('playwright.sync_api').sync_playwright() as p:
            path = p.chromium.executable_path
        import os
        if not os.path.exists(path):
            raise RuntimeError(f'未找到 Chromium：{path}')
        return path
    ok, desc = _check('chromium', _chromium)
    rows.append(('Chromium 内核（浏览器求解）', ok, desc))

    width = max(len(r[0]) for r in rows)
    all_core = all(ok for name, ok, _ in rows if '必需' in name)
    all_browser = all(ok for name, ok, _ in rows if '浏览器求解' in name)
    for name, ok, desc in rows:
        print(f'[{"OK" if ok else "缺失"}] {name.ljust(width)}  {desc}')
    print()
    if all_core and all_browser:
        print('结论: 全功能可用（图像定位 + 浏览器求解）')
    elif all_core:
        print('结论: 仅图像定位可用；浏览器求解需: pip install playwright '
              '&& python -m playwright install chromium')
    else:
        print('结论: 依赖不完整: pip install -r requirements.txt')
    sys.exit(0 if all_core else 1)


if __name__ == '__main__':
    main()
