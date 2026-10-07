#!/usr/bin/env python3
"""QI Inventory mapper — 共用引擎 + 打羊秀 profile (v0.4 draft)

Input : directory listing (json, from device_list_dir) OR a real folder path,
        folder_map csv, events csv, probe json (pdf pages / mp4 duration)
Output: xlsx (Records / Review_ByFolder / Exceptions / README) + csv
"""
import csv, json, re, sys, collections, datetime
from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter

# ----------------------------------------------------------------- profile
PROFILE = dict(
    name='打羊秀',
    series_code='04',
    level1='Dogpig Art Cafe Archive 豆皮文藝咖啡館檔案',
    level2='打羊秀',
    show_en='Show Before Closing',          # from DP2_99_04_00_01.pdf
    media_ext={'jpg': '410', 'tif': '410', 'pdf': '411', 'mp4': '412'},
    junk=('.ds_store', 'thumbs.db'),
)
MONTHS = {1: 'January', 2: 'February', 3: 'March', 4: 'April', 5: 'May', 6: 'June', 7: 'July',
          8: 'August', 9: 'September', 10: 'October', 11: 'November', 12: 'December'}
NUMW = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split()

# hand-written drafts for the 8 policy / admin documents in folder 00
DOCS_00 = {
    1: ('Show Before Closing Policy (English)', '打羊秀守則（英文版）', 'administrative paper'),
    2: ('Show Before Closing Policy', '打羊秀守則', 'administrative paper'),
    3: ('Show Before Closing Music Policy', '打羊秀音樂守則', 'administrative paper'),
    4: ('Show Before Closing Promotional Flyer', '打羊秀宣傳文字', 'ephemera'),
    5: ('Letter on Collaboration between Dogpig Art Cafe and Dagou Luan Ge Tuan', '豆皮文藝咖啡館與打狗亂歌團合作說明信', 'administrative paper\ncorrespondence'),
    6: ('Sponsorship Terms for Nicole Darcy Live CD "Peace"', '贊助 Nicole Darcy《和平》現場錄音 CD 條款', 'administrative paper'),
    7: ('Dogpig Music Publishing Contract', '豆皮音樂出版契約', 'administrative paper'),
    8: ('Schedule of Stage Events at Kaohsiung World Trade Center Book Fair', '高雄世貿書展舞台區活動節目表', 'ephemera'),
}

# ----------------------------------------------------------------- helpers
def load_folder_map(path):
    rows = list(csv.DictReader(open(path, encoding='utf-8-sig')))
    m = {}
    for r in rows:
        m[(r['level3'], r['level4'])] = (r['level4'] or r['level3'], int(r['collection_folder_id']))
    return m

def month_node(fmap, year, month):
    for (l3, l4), v in fmap.items():
        if l3 == str(year) and l4.startswith(MONTHS[month]):
            return l4, v[1]
    return None

def parse_folder(name):
    """-> dict(year, month, day, day2, rest, kind)"""
    if re.match(r'^\d{4}\s*-\s*\d{4}$', name.strip()):
        a, b = re.findall(r'\d{4}', name)
        return dict(kind='range', y1=a, y2=b, rest='')
    m = re.match(r'^(\d{4})\s*(\d{2})?\s*(?:(\d{2})(?:-(\d{2}))?)?\s*(.*)$', name)
    if not m or not m.group(2):
        return dict(kind='unknown', rest=name)
    y, mo, d, d2, rest = m.groups()
    return dict(kind='day' if d else 'month', year=int(y), month=int(mo),
                day=int(d) if d else None, day2=int(d2) if d2 else None, rest=rest.strip())

def num_word(n):
    return NUMW[n] if n < len(NUMW) else str(n)

def dur_text(sec):
    s = int(round(sec)); m, s = divmod(s, 60)
    if m and s: return f'{m} minute{"s" if m > 1 else ""} {s} second{"s" if s > 1 else ""}'
    if m: return f'{m} minute{"s" if m > 1 else ""}'
    return f'{max(s, 1)} second{"s" if s != 1 else ""}'

def hhmmss(sec):
    s = int(round(sec)); return f'{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}'

def load_events(path):
    rows = list(csv.reader(open(path, encoding='utf-8-sig')))[1:]
    ev = collections.defaultdict(list)
    for i, r in enumerate(rows, 2):
        if len(r) < 6 or not any(r[:6]): continue
        cat, name, perf = r[1], r[2].replace('\n', ' ').strip(), r[5].replace('\n', ' ')
        for fld in (r[3], r[4]):
            for d, mo, y in re.findall(r'(\d{1,2})/(\d{1,2})/(\d{4})', fld):
                try: dt = datetime.date(int(y), int(mo), int(d))
                except ValueError: continue
                if not any(e['row'] == i for e in ev[dt]):
                    ev[dt].append(dict(row=i, cat=cat, name=name, perf=perf))
    return ev

def match_event(ev, p, perf_raw):
    if p['kind'] != 'day': return None, 'none', []
    dt = datetime.date(p['year'], p['month'], p['day'])
    c = ev.get(dt, [])
    if not c: return None, 'none', []
    toks = [t for t in re.split(r'[\s、＆&()（）\-]+', perf_raw) if len(t) >= 2]
    hit = [e for e in c if any(t in e['perf'] or t in e['name'] for t in toks)]
    if hit: return hit[0], ('high' if len(hit) == 1 else 'multi'), hit
    ya = [e for e in c if '打羊秀' in e['cat']]
    if len(ya) == 1: return ya[0], 'medium', ya
    return None, 'ambiguous', c

def clean_event_name(n):
    return '' if (not n or n.startswith('無') ) else n

# ----------------------------------------------------------------- build
def build(entries, fmap, events, probe, tmpl_rows):
    P = PROFILE
    T = tmpl_rows            # first 4 rows of template csv (3 header rows + 1 example skeleton row)
    ncol = len(T[2])
    const = dict(l1=T[3][2], access=T[3][9], cr=T[3][10], rights=T[3][11], dep=T[3][12],
                 holders=T[3][13], courtesy=T[3][14], doctype=T[3][15])
    files = [e['name'].replace('\\', '/') for e in entries if e['type'] == 'file']
    rx = re.compile(r'^DP2_99_(\d\d)_(\d{6,8}|00)([a-z]?)_(\d+)([a-z]?)\.(\w+)$')
    recs, exc, groups = [], [], collections.OrderedDict()
    for f in sorted(files):
        parts = f.split('/'); base = parts[-1]; folder = parts[0]; sub = '/'.join(parts[1:-1])
        if base.lower() in P['junk'] or base.startswith('._'):
            exc.append((f, 'junk file ignored')); continue
        m = rx.match(base)
        if not m:
            exc.append((f, 'filename does not match DP2_99_{series}_{key}_{nn}')); continue
        ser, key, letter, nn, suf, ext = m.groups(); ext = ext.lower()
        if ser != P['series_code']: exc.append((f, f'series code {ser} != {P["series_code"]}')); continue
        if ext not in P['media_ext']: exc.append((f, f'unsupported ext {ext}')); continue
        recs.append(dict(f=f, folder=folder, sub=sub, base=base, key=key + letter, nn=int(nn), suf=suf, ext=ext,
                         upper=(base.rsplit('.', 1)[1] != ext)))
    recs.sort(key=lambda r: (r['key'] if r['key'] != '00' else '000000', r['nn'], r['suf']))
    out = []
    for r in recs:
        issues, drafts = [], set()
        fo = r['folder']; row = [''] * ncol
        # ---- collection
        if fo == '00':
            p = dict(kind='docs', rest=''); l3, l4, fid = 'Documents 文件', '', fmap[('Documents 文件', '')][1]
            y = None
        elif parse_folder(fo)['kind'] == 'range':
            p = parse_folder(fo); l3 = f'{p["y1"]}-{p["y2"]}'; l4 = ''; fid = fmap[(l3, '')][1]
        else:
            p = parse_folder(fo)
            if p['kind'] not in ('day', 'month'):
                exc.append((r['f'], 'folder name not parseable')); continue
            if f'{p["year"]}{p["month"]:02d}' != r['key'][:6]:
                issues.append(f'folder date {p["year"]}-{p["month"]:02d} != filename key {r["key"]}')
            l3 = str(p['year'])
            mn = month_node(fmap, p['year'], p['month'])
            if not mn: exc.append((r['f'], f'no QI month node for {p["year"]}-{p["month"]:02d}')); continue
            l4, fid = mn
        perf = p.get('rest', '')
        # ---- fixed columns
        row[0] = r['base'].rsplit('.', 1)[0] + '.' + r['ext']
        row[1] = P['media_ext'][r['ext']]
        row[2], row[3], row[4], row[5], row[8] = const['l1'], P['level2'], l3, l4, fid
        row[9], row[10], row[11], row[12], row[13], row[14], row[15] = (const['access'], const['cr'], const['rights'],
            const['dep'], const['holders'], const['courtesy'], const['doctype'])
        row[19] = 'Allocated'; row[21] = 'Chinese - Traditional'
        # ---- dates
        if p['kind'] == 'day':
            row[29] = f'{p["year"]}-{p["month"]:02d}-{p["day"]:02d}'; row[30] = '0'
            if p['day2']: row[31] = f'{p["year"]}-{p["month"]:02d}-{p["day2"]:02d}'; row[32] = '0'
        elif p['kind'] == 'month':
            row[29] = f'{p["year"]}-{p["month"]:02d}-__'; row[30] = '1'; issues.append('date only to month (est=1)')
        elif p['kind'] == 'range':
            row[29] = f'{p["y1"]}-__-__'; row[30] = '1'; row[31] = f'{p["y2"]}-__-__'; row[32] = '1'
            issues.append('folder is a year range 2002–2004; exact dates unknown')
        else:
            issues.append('no date'); drafts.add(29)
        # ---- event
        ev, conf, cands = match_event(events, p, perf)
        if ev:
            row[72] = clean_event_name(ev['name']); drafts.add(72)
            if '豬頭劇' in ev['cat'] or '混咬' in ev['cat']: issues.append(f'cross_series: event category = {ev["cat"]}')
            if conf != 'high': issues.append(f'event match {conf}')
            if not row[72]: issues.append('matched event has no name')
        elif p['kind'] == 'day': issues.append(f'no event row on this date ({conf})')
        # ---- people
        if perf and fo != '00':
            row[73] = perf; row[74] = 'Artist'; drafts.update({73, 74})
        # ---- type-specific
        pr = probe.get(r['f'].replace('/', '/'), {})
        who = perf or ('Show Before Closing' if fo != '00' else '')
        show = P['show_en']
        if r['ext'] in ('jpg', 'tif'):
            row[16] = 'event photograph/recording'
            row[18] = f'Photo of {who} Performing at {show}' if perf else f'Photo of a Performance at {show}'
            row[20] = f'{perf}打羊秀表演照片' if perf else '打羊秀表演照片'
            drafts.update({16, 18, 20})
            if r['ext'] == 'tif': issues.append('tif: confirm whether this is a converted file (source format / date)')
        elif r['ext'] == 'mp4':
            row[16] = 'event photograph/recording'
            row[18] = f'Video of {who} Performing at {show}' if perf else f'Video of a Performance at {show}'
            row[20] = f'{perf}打羊秀表演影像' if perf else '打羊秀表演影像'
            drafts.update({16, 18, 20})
            d = probe.get(r['f'], {}).get('dur')
            if d is not None:
                row[23] = f'This video documentation runs for {dur_text(d)} long.'; row[68] = hhmmss(d)
            else: issues.append('video duration unknown')
        elif r['ext'] == 'pdf':
            pg = probe.get(r['f'], {}).get('pages'); tx = probe.get(r['f'], {}).get('text', '')
            if pg and pg > 1: row[23] = f'This PDF file contains {num_word(pg)} pages.'
            if fo == '00':
                n = int(r['nn']); en, zh, ct = DOCS_00[n]
                row[18], row[20], row[16] = en, zh, ct
            elif tx.startswith('From'):
                sender = re.match(r'From:?\s*([^<]+?)\s*(?:<|To)', tx)
                sender = sender.group(1).strip() if sender else 'sender'
                subj = re.search(r'Subject:?\s*(.{0,60})', tx)
                row[16] = 'administrative paper\ncorrespondence'
                row[18] = f'Email from {sender} to Dogpig Art Cafe'
                row[20] = f'{sender}寄給豆皮文藝咖啡館的電子郵件'
                if perf: row[18] += f' ({perf})'; row[20] += f'（{perf}）'
            else:
                bio = bool(re.search(r'團名|團員|簡介|Name:|I am|自我|樂團.{0,6}(組成|成立)', tx))
                row[16] = 'biography' if bio else 'press material'
                row[18] = f'{"Profile" if bio else "Promotional Text"} of {who}'
                row[20] = f'{perf}{"簡介" if bio else "宣傳文字"}'
            drafts.update({16, 18, 20})
        if r['sub']: issues.append(f'subfolder: {r["sub"]}')
        if r['upper']: issues.append('file extension is upper-case on disk; output lower-case')
        out.append(dict(row=row, drafts=drafts, issues=issues, rec=r, ev=ev, conf=conf, perf=perf, folder=fo))
    return out, exc

# ----------------------------------------------------------------- write
def write(out, exc, tmpl_rows, xlsx_path, csv_path):
    wb = Workbook(); ws = wb.active; ws.title = 'Records'
    f_ = Font(name='Arial', size=10); fb = Font(name='Arial', size=10, bold=True)
    yellow = PatternFill('solid', fgColor='FFF2CC'); orange = PatternFill('solid', fgColor='F8CBAD')
    for i, h in enumerate(tmpl_rows[:3], 1):
        for j, v in enumerate(h, 1):
            c = ws.cell(i, j, v); c.font = fb; c.alignment = Alignment(wrap_text=True, vertical='top')
    for i, o in enumerate(out, 4):
        for j, v in enumerate(o['row'], 1):
            if v == '': continue
            c = ws.cell(i, j, v); c.font = f_
            if (j - 1) in o['drafts']: c.fill = yellow
        # required-but-empty
        for j in (29,):
            if not o['row'][j]: ws.cell(i, j + 1).fill = orange
    ws.freeze_panes = 'B4'
    for j in range(1, len(tmpl_rows[2]) + 1):
        ws.column_dimensions[get_column_letter(j)].width = 22
    # review by folder
    rv = wb.create_sheet('Review_ByFolder')
    hdr = ['Folder', 'Key', 'Files', 'Date (QI)', 'Performer (from folder)', 'Matched event (D/M/Y table)', 'Event category',
           'Match', 'Issues', 'Draft title EN (sample)', 'Draft title ZH (sample)', 'Reviewed? (Y/N)', 'Reviewer notes']
    for j, h in enumerate(hdr, 1): rv.cell(1, j, h).font = fb
    g = collections.OrderedDict()
    for o in out: g.setdefault(o['folder'], []).append(o)
    for i, (fo, lst) in enumerate(g.items(), 2):
        o = lst[0]; iss = collections.Counter(x for l in lst for x in l['issues'] if not x.startswith('tif') )
        vals = [fo, o['rec']['key'], len(lst), o['row'][29] + (f' → {o["row"][31]}' if o['row'][31] else ''), o['perf'],
                (o['ev'] or {}).get('name', ''), (o['ev'] or {}).get('cat', ''), o['conf'],
                '; '.join(f'{k} ×{v}' if v < len(lst) else k for k, v in iss.items()),
                o['row'][18], o['row'][20], '', '']
        for j, v in enumerate(vals, 1): rv.cell(i, j, v).font = f_
        if o['conf'] in ('none', 'ambiguous', 'multi') or any('cross_series' in k for k in iss):
            for j in (6, 8, 9): rv.cell(i, j).fill = orange
    for j, w in enumerate([46, 10, 7, 24, 34, 28, 18, 10, 60, 50, 36, 12, 30], 1):
        rv.column_dimensions[get_column_letter(j)].width = w
    rv.freeze_panes = 'A2'
    ex = wb.create_sheet('Exceptions')
    ex.cell(1, 1, 'Path').font = fb; ex.cell(1, 2, 'Reason').font = fb
    for i, (p, r) in enumerate(exc, 2): ex.cell(i, 1, p).font = f_; ex.cell(i, 2, r).font = f_
    ex.column_dimensions['A'].width = 90; ex.column_dimensions['B'].width = 60
    rd = wb.create_sheet('README')
    for i, t in enumerate(['QI Inventory — 打羊秀 dry-run output (profile: 打羊秀 / series 04)',
        'Records: same 79 columns / 3 header rows as the QI template. One row per media file.',
        'Yellow cell = drafted by rule, needs human review. Orange cell = missing or ambiguous.',
        'Drafts are built from folder names, the 豆皮 Event table and file metadata (PDF text, video length). Photo content was NOT inspected.',
        'Review_ByFolder: one line per performer folder; fill "Reviewed?" as you go.',
        'Exceptions: files skipped and why.'], 1):
        rd.cell(i, 1, t).font = fb if i == 1 else f_
    rd.column_dimensions['A'].width = 130
    wb.save(xlsx_path)
    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as fh:
        w = csv.writer(fh)
        for h in tmpl_rows[:3]: w.writerow(h)
        for o in out: w.writerow(o['row'])

if __name__ == '__main__':
    base = '/home/claude/qi/'
    entries = json.load(open(base + 'yangxiu_entries.json', encoding='utf-8'))
    fmap = load_folder_map(base + 'folder_map_打羊秀.csv')
    events = load_events(base + 'events_raw.csv')
    probe = json.load(open(base + 'probe.json', encoding='utf-8'))
    tmpl = list(csv.reader(open('/mnt/user-data/uploads/QI_Inventory_豆皮_打羊秀Records.csv', encoding='utf-8-sig')))[:4]
    out, exc = build(entries, fmap, events, probe, tmpl)
    write(out, exc, tmpl, base + '打羊秀_QI_dryrun.xlsx', base + '打羊秀_QI_dryrun.csv')
    print('records', len(out), 'exceptions', len(exc))
