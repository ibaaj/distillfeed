"""Focused browser regression for arXiv configuration hover/focus contrast.

Run from the release root:
    python tests/browser_arxiv_config_rc8.py
Requires Playwright and Chromium. Set CHROMIUM to a system binary if needed.
Set RC8_SCREENSHOT_DIR to capture visual evidence. Does not call the network.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "rss_reader/static/app.css").read_text(encoding="utf-8")
TEMPLATE = (ROOT / "rss_reader/templates/index.html").read_text(encoding="utf-8")
match = re.search(r'<details id="arxiv-scope-config".*?</details>', TEMPLATE, re.S)
assert match, "missing arXiv config menu in actual template"
menu = match.group(0).replace("{{ scope_title }}", "arXiv Digest")
assert "Open arXiv configuration" in menu

# Render the exact arXiv menu markup and production stylesheet; isolate CSS
# specificity and state transitions from Flask and from external services.
HTML = f'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1" />
<style>{CSS}</style></head><body class="reader-page">
<div style="display:flex;justify-content:flex-end;padding:12px">
<div class="review-heading-actions">{menu}</div></div></body></html>'''


def contrast(foreground: str, background: str) -> float:
    def luminance(value: str) -> float:
        channels = [int(v) / 255 for v in re.findall(r"\d+", value)[:3]]
        assert len(channels) == 3, value
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
        return sum(x * weight for x, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
    light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def style(button):
    return button.evaluate('''node => {
      const css = getComputedStyle(node);
      const box = node.getBoundingClientRect();
      const rgb = value => {
        const canvas = document.createElement('canvas');
        canvas.width = canvas.height = 1;
        const ctx = canvas.getContext('2d');
        ctx.fillStyle = value;
        ctx.fillRect(0, 0, 1, 1);
        const pixel = ctx.getImageData(0, 0, 1, 1).data;
        return `rgb(${pixel[0]}, ${pixel[1]}, ${pixel[2]})`;
      };
      return {
        background: rgb(css.backgroundColor), text: rgb(css.color),
        outline: css.outlineStyle, outlineWidth: css.outlineWidth,
        right: box.right, left: box.left, width: box.width,
      };
    }''')


def main() -> None:
    statuses = []
    with sync_playwright() as p:
        chrome = os.environ.get("CHROMIUM")
        launch_options = {"headless": True}
        if chrome:
            launch_options["executable_path"] = chrome
            launch_options["args"] = ["--no-sandbox"]
        browser = p.chromium.launch(**launch_options)
        for width in (375, 1440):
            for dark in (False, True):
                page = browser.new_page(viewport={"width": width, "height": 760})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.set_content(HTML)
                if dark:
                    page.locator("body").evaluate('(el) => el.classList.add("dark-mode")')
                summary = page.locator('#arxiv-scope-config > summary')
                summary.click()
                assert page.locator('#arxiv-scope-config').get_attribute('open') is not None
                button = page.locator('#arxiv-scope-config > .arxiv-config-popover > button')
                assert button.is_visible()

                normal = style(button)
                button.hover()
                hovered = style(button)
                assert hovered['background'] != normal['background'], (width, dark, normal, hovered)
                assert contrast(hovered['text'], hovered['background']) >= 4.5, (width, dark, hovered)
                assert contrast(normal['text'], normal['background']) >= 4.5, (width, dark, normal)
                assert 0 <= hovered['left'] < hovered['right'] <= width + 1, (width, dark, hovered)

                summary.focus()
                page.keyboard.press('Tab')
                assert button.evaluate('(node) => node === document.activeElement'), 'Keyboard Tab did not reach the button'
                focused = style(button)
                assert focused['outline'] != 'none' and focused['outlineWidth'] != '0px', (width, dark, focused)
                assert contrast(focused['text'], focused['background']) >= 4.5, (width, dark, focused)

                button.click()
                assert not errors, errors
                screenshot_dir = os.environ.get("RC8_SCREENSHOT_DIR")
                if screenshot_dir:
                    output = Path(screenshot_dir)
                    output.mkdir(parents=True, exist_ok=True)
                    filename = f"rc8-arxiv-config-{'dark' if dark else 'light'}-{width}.png"
                    page.screenshot(path=str(output / filename))
                statuses.append({"width": width, "dark": dark,
                                 "normal_contrast": round(contrast(normal['text'],normal['background']), 2),
                                 "hover_contrast": round(contrast(hovered['text'],hovered['background']), 2),
                                 "focus_contrast": round(contrast(focused['text'],focused['background']), 2)})
                page.close()
        browser.close()
    print(json.dumps({"result": "PASS", "cases": statuses}, indent=2))

if __name__ == '__main__':
    main()
