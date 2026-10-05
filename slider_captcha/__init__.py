# -*- coding: utf-8 -*-
"""滑块验证码求解库。

只做图像定位（不装 opencv 也能 import 本包，调用时才要求依赖）：
    from slider_captcha import find_gap, Gap          # 缺口定位（需 numpy+opencv）
    from slider_captcha import human_track            # 拟人轨迹（纯 Python）

在已有 Playwright 页面上求解（浏览器驱动为可插拔适配器，见 page_driver）：
    from slider_captcha import solve_on_page          # async 页面
    result = await solve_on_page(page)                # -> SolveResult
    from slider_captcha import solve_on_page_sync     # sync 页面

环境自检：python -m slider_captcha.doctor
"""
__version__ = '2.1.0'

__all__ = ['Gap', 'find_gap', 'human_track', 'Tuning', 'Selectors', 'SolveResult',
           'solve_on_page', 'solve_on_page_sync', 'demo_url', '__version__']


def __getattr__(name):
    # PEP 562 惰性导出：import 本包不触发 numpy/cv2/playwright 加载
    if name in ('Gap', 'find_gap'):
        from . import detector
        return getattr(detector, name)
    if name == 'human_track' or name == 'Tuning':
        from . import trajectory
        return getattr(trajectory, name)
    if name in ('Selectors', 'SolveResult', 'solve_on_page', 'solve_on_page_sync'):
        from . import page_driver
        return getattr(page_driver, name)
    if name == 'demo_url':
        from .runner import demo_url
        return demo_url
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
