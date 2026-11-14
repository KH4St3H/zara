"""Markdown (CommonMark-ish / Mattermost / Claude output) -> Telegram HTML.

Telegram supports only: b, i, u, s, code, pre, a, blockquote. Everything else is
flattened (headings -> bold, bullets -> •, tables -> monospace block).
"""
import html
import re

_PH = "\x00%d\x00"


def _inline(text: str) -> str:
    keep = []

    def stash(s):
        keep.append(s)
        return _PH % (len(keep) - 1)

    # inline code first, so nothing inside it is formatted
    text = re.sub(r"`([^`\n]+)`", lambda m: stash("<code>%s</code>" % html.escape(m.group(1))), text)
    # links [text](url) and <url>
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)",
                  lambda m: stash('<a href="%s">%s</a>' % (html.escape(m.group(2), quote=True),
                                                         html.escape(m.group(1)))), text)
    text = re.sub(r"<(https?://[^>\s]+)>", lambda m: stash(html.escape(m.group(1))), text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(?=\S)(.+?)(?<=\S)__", r"<b>\1</b>", text)
    text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<s>\1</s>", text)
    text = re.sub(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])", r"<i>\1</i>", text)
    return re.sub("\x00(\\d+)\x00", lambda m: keep[int(m.group(1))], text)


def to_html(md: str) -> str:
    out, lines, i = [], md.replace("\r\n", "\n").split("\n"), 0
    while i < len(lines):
        line = lines[i]
        fence = re.match(r"^\s*```\s*([\w+-]*)\s*$", line)
        if fence:  # fenced code block
            lang, body, i = fence.group(1), [], i + 1
            while i < len(lines) and not re.match(r"^\s*```\s*$", lines[i]):
                body.append(lines[i])
                i += 1
            code = html.escape("\n".join(body))
            out.append('<pre><code class="language-%s">%s</code></pre>' % (lang, code) if lang
                       else "<pre>%s</pre>" % code)
            i += 1
            continue
        if line.lstrip().startswith("|"):  # table -> monospace
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                if not re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i]):
                    rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            widths = [max(len(r[c]) if c < len(r) else 0 for r in rows) for c in range(max(map(len, rows)))]
            text = "\n".join("  ".join((r[c] if c < len(r) else "").ljust(widths[c])
                                       for c in range(len(widths))).rstrip() for r in rows)
            out.append("<pre>%s</pre>" % html.escape(text))
            continue
        h = re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", line)
        if h:
            out.append("<b>%s</b>" % _inline(h.group(1)))
        elif re.match(r"^\s*([-*_])\s*(\1\s*){2,}$", line):
            out.append("──────────")
        elif re.match(r"^\s*>\s?", line):
            quote = []
            while i < len(lines) and re.match(r"^\s*>\s?", lines[i]):
                quote.append(_inline(re.sub(r"^\s*>\s?", "", lines[i])))
                i += 1
            out.append("<blockquote>%s</blockquote>" % "\n".join(quote))
            continue
        else:
            b = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
            if b:
                out.append("%s• %s" % ("  " * (len(b.group(1)) // 2), _inline(b.group(2))))
            else:
                out.append(_inline(line))
        i += 1
    return "\n".join(out)


def chunks(md: str, limit: int = 3500):
    """Split markdown on paragraph/line boundaries (never inside a code fence when avoidable)."""
    parts, cur = [], ""
    for para in re.split(r"(\n\n)", md):
        if len(cur) + len(para) > limit and cur:
            parts.append(cur)
            cur = ""
        while len(para) > limit:
            cut = para.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            parts.append(para[:cut])
            para = para[cut:]
        cur += para
    if cur.strip():
        parts.append(cur)
    # keep code fences balanced per chunk
    fixed, carry = [], False
    for p in parts:
        if carry:
            p = "```\n" + p
        carry = p.count("```") % 2 == 1
        fixed.append(p + ("\n```" if carry else ""))
    return fixed or [""]
