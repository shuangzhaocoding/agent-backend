# -*- coding: utf-8 -*-
#
# SOP 富文本 HTML → Markdown
#
import markdownify


def html_to_markdown(html: str) -> str:
    if not html or not str(html).strip():
        return ""
    return markdownify.markdownify(
        str(html),
        heading_style="ATX",
        bullets="-",
        strip=["script", "style"],
    ).strip()
