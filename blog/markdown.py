"""
✍️ محوّل Markdown صغير وآمن للمقالات (من غير مكتبات).

كل النص بيتعمله escape الأول، وبعدين بنحوّل صيغ محدودة بس:
    ## عنوان / ### عنوان فرعي — فقرات — - قوايم — **عريض** — [نص](/مسار أو https://...)
    وجداول بسيطة (| a | b |). أي HTML مكتوب في المقال بيظهر كنص (مفيش حقن).
"""
import html
import re

_LINK = re.compile(r'\[([^\]]+)\]\(((?:/|https?://)[^)\s]*)\)')
_BOLD = re.compile(r'\*\*(.+?)\*\*')


def _inline(text):
    s = html.escape(text, quote=True)

    def link(m):
        url = m.group(2)
        ext = url.startswith('http')
        attrs = ' target="_blank" rel="noopener nofollow"' if ext else ''
        return f'<a href="{url}"{attrs}>{m.group(1)}</a>'
    s = _LINK.sub(link, s)
    return _BOLD.sub(r'<strong>\1</strong>', s)


def _slug(text, used):
    base = re.sub(r'[^\w\u0600-\u06FF]+', '-', text).strip('-')[:60] or 'sec'
    slug, i = base, 2
    while slug in used:
        slug, i = f'{base}-{i}', i + 1
    used.add(slug)
    return slug


def render(text):
    """بيرجّع (HTML، فهرس المحتويات [(id، عنوان)])."""
    out, toc, used = [], [], set()
    lines = (text or '').replace('\r\n', '\n').split('\n')
    para, items, table = [], [], []

    def flush():
        if para:
            out.append('<p>' + _inline(' '.join(para)) + '</p>')
            para.clear()
        if items:
            out.append('<ul>' + ''.join(f'<li>{_inline(x)}</li>' for x in items) + '</ul>')
            items.clear()
        if table:
            rows = [r for r in table if not re.fullmatch(r'\|?[\s:\-|]+\|?', r)]
            cells = [[c.strip() for c in r.strip().strip('|').split('|')] for r in rows]
            if cells:
                head = ''.join(f'<th>{_inline(c)}</th>' for c in cells[0])
                body = ''.join('<tr>' + ''.join(f'<td>{_inline(c)}</td>' for c in r) + '</tr>' for r in cells[1:])
                out.append(f'<div class="tbl"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')
            table.clear()

    for raw in lines:
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.startswith('|'):
            if para or items:
                flush()
            table.append(line)
            continue
        if table:
            flush()
        m = re.match(r'(#{2,3})\s+(.*)', line)
        if m:
            flush()
            level = len(m.group(1))
            sid = _slug(m.group(2), used)
            if level == 2:
                toc.append((sid, m.group(2)))
            out.append(f'<h{level} id="{sid}">{_inline(m.group(2))}</h{level}>')
            continue
        if line.startswith(('- ', '* ')):
            if para:
                out.append('<p>' + _inline(' '.join(para)) + '</p>')
                para.clear()
            items.append(line[2:])
            continue
        if items:
            flush()
        para.append(line)
    flush()
    return '\n'.join(out), toc


def plain(text):
    """النص من غير صيغ — لعدد الكلمات / الوصف."""
    t = _LINK.sub(r'\1', text or '')
    t = re.sub(r'[#*|>]', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()
