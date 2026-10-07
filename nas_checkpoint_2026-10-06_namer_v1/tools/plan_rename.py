import csv, re, sys, collections, unicodedata, os
sys.path.insert(0,'tool')
from nas_rename.folderparse import parse_folder_name, folder_sort_key, nfc_fold
PREFIX="DP2_99_04_"
DP=re.compile(r'^DP2_99_04_(\d{6})([a-z]+)_(\d+)$',re.I)
DP00=re.compile(r'^DP2_99_04_00_(\d+)$',re.I)
ROOT="Z:\\18_豆皮藝文咖啡廳第二期\\數位檔案_整理\\04 打羊秀(2000-2005)"
rows=list(csv.DictReader(open('nas_listing_now.csv',encoding='utf-8-sig')))
def split(x):
    rel=x['FullName'][len(ROOT):].lstrip('\\').split('\\')
    return rel
files=[]
for x in rows:
    rel=split(x); name=rel[-1]; stem,ext=os.path.splitext(name)
    files.append(dict(rel=tuple(rel),folder=tuple(rel[:-1]),stem=stem,ext=ext,size=int(x['Length'])))
groups=collections.defaultdict(list)
for f in files: groups[f['folder']].append(f)

def resolve(fp):
    if not fp: return None,(),False
    own=parse_folder_name(fp[-1])
    if own.ok: return own,fp,False
    for i in range(len(fp)-1,0,-1):
        c=parse_folder_name(fp[i-1])
        if c.ok: return c,fp[:i],True
    return own,(),False

def is_att(fp): return any(p.endswith('_attachments') for p in fp)
units={}
for fp,fl in groups.items():
    if is_att(fp) or fp==('00',): continue
    fd,src,inh=resolve(fp)
    ex=set()
    for f in fl:
        m=DP.match(f['stem'])
        if m: ex.add((m.group(1),m.group(2).lower()))
    units[fp]=dict(fd=fd,src=src,inh=inh,ex=ex,letter=None,conflict='')
# conflicts
claims=collections.defaultdict(list)
for fp,u in units.items():
    for k in u['ex']: claims[k].append(fp)
    if len(u['ex'])>1: u['conflict']='MULTI_LETTER'
    elif u['ex'] and u['fd'].ok:
        k=next(iter(u['ex']))
        if k[0]!=u['fd'].yyyymm: u['conflict']=f'YYYYMM_MISMATCH folder={u["fd"].yyyymm} name={k[0]}'
for k,fps in claims.items():
    if len(fps)>1:
        allfiles=[f for fp in fps for f in groups[fp]]
        stems=[f['stem'].casefold() for f in allfiles]
        pure=all(DP.match(f['stem']) for f in allfiles) and len(set(stems))==len(stems)
        for fp in fps:
            if pure: units[fp]['shared']=k
            else: units[fp]['conflict']=units[fp]['conflict'] or f'LETTER_SHARED {k}'
occ=collections.defaultdict(set)
for (ym,l) in claims: occ[ym].add(l)
for fp,u in units.items():
    if len(u['ex'])==1 and not u['conflict']: u['letter']=next(iter(u['ex']))[1]
# ---- v2 決策：混咬表演子資料夾全部共用一個字母；序號跨子資料夾連續 ----
SHARE_PARENT=('2004 01 混咬表演',)
def share_key(fp):
    return SHARE_PARENT if fp[:1]==SHARE_PARENT and len(fp)>1 else None
new=collections.defaultdict(list)
sep='\\'
shared_units=collections.defaultdict(list)
for fp,u in units.items():
    if u['fd'].ok and not u['ex'] and not u['conflict']:
        sk=share_key(fp)
        if sk: shared_units[sk].append(fp)
        else: new[u['fd'].yyyymm].append(([fp],u))
for sk,fps in shared_units.items():
    u0=units[fps[0]]
    new[u0['fd'].yyyymm].append((sorted(fps,key=lambda p:(nfc_fold(sep.join(p)),sep.join(p))),u0))
for ym,us in new.items():
    us.sort(key=lambda t: folder_sort_key(t[1]['fd'], sep.join(t[0][0]) if t[1]['inh'] else t[0][0][-1], sep.join(t[0][0])))
    for fps,u in us:
        for l in 'abcdefghijklmnopqrstuvwxyz':
            if l not in occ[ym]:
                occ[ym].add(l)
                for fp in fps: units[fp]['letter']=l
                break
def fkey(f): return (nfc_fold(f['stem']),nfc_fold(f['ext']),f['stem'],f['ext'])
out=[]
def stem_ext(f): return f['stem']+f['ext']
# ---- 序號：以「分配單位」為單位（一般＝單一資料夾；混咬表演＝全部子資料夾）----
alloc=collections.defaultdict(list)    # key -> [files in plan order]
for fp,fl in groups.items():
    if fp in units and units[fp]['letter']:
        sk=share_key(fp)
        key=('SHARE',sk) if sk else ('F',fp)
        alloc[key].append(fp)
seq={}
for key,fps in alloc.items():
    fps=sorted(fps,key=lambda p:(nfc_fold(sep.join(p)),sep.join(p)))
    allf=[f for fp in fps for f in groups[fp]]
    used=set()
    for f in allf:
        m=DP.match(f['stem'])
        if m: seq[id(f)]=int(m.group(3)); used.add(int(m.group(3)))
    n=1
    for fp in fps:
        for f in sorted(groups[fp],key=fkey):
            if id(f) in seq: continue
            while n in used: n+=1
            seq[id(f)]=n; used.add(n)
# ---- 2002   -   2004 ----
SP=('2002   -   2004',)
n=1
for f in sorted(groups.get(SP,[]),key=fkey):
    seq[id(f)]=n; n+=1
# ---- attachments → 併入的 PDF ----
att_map={}
for fp in groups:
    if fp and fp[-1].endswith('_attachments'):
        parent=fp[:-1]; base=fp[-1][:-len('_attachments')]
        att_map[fp]=(parent,base)
for fp,fl in groups.items():
    u=units.get(fp)
    if fp==('00',):
        for f in fl: out.append((f,stem_ext(f),'KEEP','EXISTING_DP2_00 不改名'))
        continue
    if fp in att_map:
        parent,base=att_map[fp]
        for f in fl:
            if f['ext'].lower()=='.db': out.append((f,stem_ext(f),'DELETE','系統檔，Emily 指示刪除（Z: 無資源回收桶）'))
            else: out.append((f,stem_ext(f),'MERGE_SOURCE',f'併入 {base}.pdf（見 merge_plan.csv）；不占 DP2 序號、不改名；合併驗證後移除'))
        continue
    if fp==SP:
        for f in fl:
            out.append((f,f"{PREFIX}20022004_{seq[id(f)]:02d}{f['ext'].lower()}",'RENAME','ASSIGNED 20022004 系列（Emily 指定，無字母）'))
        continue
    if not u['fd'].ok:
        for f in fl: out.append((f,stem_ext(f),'NEED_REVIEW',f'日期無法判定:{u["fd"].reason}，不猜'))
        continue
    if u['conflict'] or not u['letter']:
        for f in fl: out.append((f,stem_ext(f),'NEED_REVIEW',u['conflict'] or 'LETTER_OVERFLOW'))
        continue
    ym=u['fd'].yyyymm; L=u['letter']
    for f in sorted(fl,key=fkey):
        ext=f['ext'].lower()
        m=DP.match(f['stem'])
        if m: newstem=f['stem']; kind='EXISTING'
        else: newstem=f"{PREFIX}{ym}{L}_{seq[id(f)]:02d}"; kind='ASSIGNED'
        new_name=newstem+ext
        action='KEEP' if new_name==stem_ext(f) else 'RENAME'
        note=kind
        if u.get('shared'): note+=' [SHARED_LETTER %s%s]'%u['shared']
        if share_key(fp): note+=f' [混咬表演共用 {ym}{L}，序號跨子資料夾連續]'
        elif u['inh']: note+=' (inherit '+'\\'.join(u['src'])+')'
        out.append((f,new_name,action,note))
import pickle
pickle.dump((out,units,att_map),open('plan.pkl','wb'))
print(len(out)); print(collections.Counter(o[2] for o in out))
