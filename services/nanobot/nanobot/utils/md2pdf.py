#!/usr/bin/env python3
"""md2pdf <input.md> [output.pdf] — render a Markdown report to a tidy PDF.

Pure Python (reportlab), headless-safe. Supports headings, paragraphs,
bullet/number lists, tables, blockquotes, and inline **bold** / *italic* /
`code` / [links](url). Used by research/report subagents: write a report.md,
run `md2pdf report.md report.pdf`, then link the file in the reply.
"""
import html
import re
import sys

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, ListFlowable, ListItem)

_BOLD = re.compile(r'\*\*(.+?)\*\*')
_ITAL = re.compile(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)')
_CODE = re.compile(r'`([^`]+)`')
_LINK = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')


def _inline(text):
    """Markdown inline -> reportlab mini-HTML (escaped)."""
    text = html.escape(text, quote=False)
    text = _CODE.sub(lambda m: f'<font face="Courier">{m.group(1)}</font>', text)
    text = _BOLD.sub(r'<b>\1</b>', text)
    text = _ITAL.sub(r'<i>\1</i>', text)
    text = _LINK.sub(lambda m: f'<a href="{m.group(2)}" color="#2b6cb0">{m.group(1)}</a>', text)
    return text


def _styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle('Body2', parent=ss['BodyText'], fontSize=10.5, leading=15, spaceAfter=6))
    for i, sz in ((1, 20), (2, 15), (3, 12.5)):
        ss.add(ParagraphStyle('H%dx' % i, parent=ss['Heading%d' % i], fontSize=sz,
                              textColor=colors.HexColor('#2f5233'), spaceBefore=12, spaceAfter=6))
    ss.add(ParagraphStyle('Quote2', parent=ss['BodyText'], leftIndent=14, textColor=colors.HexColor('#555'),
                          fontSize=10, leading=14, spaceAfter=6))
    return ss


def md_to_flowables(md, ss):
    out, i, lines = [], 0, md.replace('\r\n', '\n').split('\n')
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1; continue
        m = re.match(r'(#{1,3})\s+(.*)', s)
        if m:
            out.append(Paragraph(_inline(m.group(2)), ss['H%dx' % len(m.group(1))])); i += 1; continue
        if s.startswith('>'):
            out.append(Paragraph(_inline(s.lstrip('> ').rstrip()), ss['Quote2'])); i += 1; continue
        if '|' in s and i + 1 < len(lines) and re.match(r'^\s*\|?[\s:|-]+\|?\s*$', lines[i + 1]):
            rows = []
            while i < len(lines) and '|' in lines[i]:
                stripped = lines[i].strip().strip('|')
                if re.match(r'^[\s:|-]+$', stripped):
                    i += 1; continue
                cells = [c.strip() for c in stripped.split('|')]
                rows.append([Paragraph(_inline(c), ss['Body2']) for c in cells]); i += 1
            if rows:
                t = Table(rows, hAlign='LEFT')
                t.setStyle(TableStyle([
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5c0')),
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eaf0e4')),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('LEFTPADDING', (0, 0), (-1, -1), 5), ('RIGHTPADDING', (0, 0), (-1, -1), 5),
                    ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3)]))
                out.append(t); out.append(Spacer(1, 6))
            continue
        m = re.match(r'^([-*+]|\d+[.)])\s+(.*)', s)
        if m:
            ordered = m.group(1)[0] not in '-*+'
            items = []
            while i < len(lines):
                mm = re.match(r'^\s*([-*+]|\d+[.)])\s+(.*)', lines[i])
                if not mm:
                    break
                items.append(ListItem(Paragraph(_inline(mm.group(2)), ss['Body2']), leftIndent=12))
                i += 1
            out.append(ListFlowable(items, bulletType='1' if ordered else 'bullet',
                                    bulletColor=colors.HexColor('#2f5233'))); continue
        para = [s]; i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r'(#{1,3}\s|[-*+]\s|\d+[.)]\s|>|\|)', lines[i].strip()):
            para.append(lines[i].strip()); i += 1
        out.append(Paragraph(_inline(' '.join(para)), ss['Body2']))
    return out


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print('usage: md2pdf <input.md> [output.pdf]', file=sys.stderr); return 2
    src = argv[0]
    dst = argv[1] if len(argv) > 1 else re.sub(r'\.md$', '', src) + '.pdf'
    with open(src, encoding='utf-8') as f:
        md = f.read()
    ss = _styles()
    doc = SimpleDocTemplate(dst, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm,
                            topMargin=1.8 * cm, bottomMargin=1.8 * cm, title=src)
    doc.build(md_to_flowables(md, ss) or [Paragraph('(vacio)', ss['BodyText'])])
    print(dst)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
