import json, os, re, sqlite3, time, uuid, webbrowser
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TPE2, TALB, TDRC, TRCK, TCON, TXXX, USLT, APIC
from mutagen.mp3 import MP3

APP_NAME = 'Music Metadata Researcher'
APP_VERSION = 'MVP Beta 1'
APP_DIR = Path(os.getenv('APPDATA', Path.home())) / 'MusicMetadataResearcher'
DB = APP_DIR / 'knowledge.sqlite3'
APP_DIR.mkdir(parents=True, exist_ok=True)
MB_BASE = 'https://musicbrainz.org/ws/2'
UA = 'MusicMetadataResearcher/1.0 (local desktop app)'
MAX_WORKERS = 2

def http_json(url):
    req = Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
    with urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode('utf-8'))

def norm(s): return re.sub(r'\s+', ' ', (s or '').strip()).casefold()
def parse_filename(path):
    stem = Path(path).stem
    if ' - ' in stem: return tuple(x.strip() for x in stem.split(' - ', 1))
    return '', stem.strip()
def file_identity(path):
    p=Path(path); st=p.stat(); return {'path':str(p.resolve()),'size':int(st.st_size),'mtime_ns':int(st.st_mtime_ns)}
def audio_duration_ms(path): return round(MP3(path).info.length*1000)

def tag_values(path):
    out={}
    try: tags=ID3(path)
    except Exception: return out
    for key,field in [('TPE1','artist'),('TPE2','album_artist'),('TIT2','title'),('TALB','album'),('TDRC','year'),('TRCK','track'),('TCON','genre')]:
        if key in tags and getattr(tags[key],'text',None): out[field]=str(tags[key].text[0])
    for frame in tags.getall('TXXX'):
        if frame.desc and frame.desc.casefold()=='country' and frame.text: out['country']=str(frame.text[0])
    try: out['lyrics']=next(iter(tags.getall('USLT'))).text
    except Exception: pass
    return out

def extract_front_cover(path):
    try: tags=ID3(path)
    except Exception: return None
    for frame in tags.getall('APIC'):
        if frame.type==3 and frame.data:
            d=APP_DIR/'artwork_cache'; d.mkdir(parents=True,exist_ok=True)
            ext='.jpg' if 'jpeg' in frame.mime.lower() or 'jpg' in frame.mime.lower() else '.bin'
            p=d/f'{uuid.uuid4().hex}{ext}'; p.write_bytes(frame.data)
            return {'path':str(p),'mime':frame.mime,'source':'Existing MP3 artwork'}
    return None

def connect():
    con=sqlite3.connect(DB); con.row_factory=sqlite3.Row; return con

def init_db():
    with connect() as c:
        c.executescript('''PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS research_items (id TEXT PRIMARY KEY,created_at REAL NOT NULL,updated_at REAL NOT NULL,path TEXT,size INTEGER,mtime_ns INTEGER,input_artist TEXT,input_title TEXT,status TEXT NOT NULL,attention TEXT,confidence INTEGER,refresh INTEGER DEFAULT 0,generation INTEGER DEFAULT 1,system_json TEXT DEFAULT '{}',overrides_json TEXT DEFAULT '{}',final_json TEXT DEFAULT '{}',recording_json TEXT DEFAULT '{}',release_json TEXT DEFAULT '{}',candidates_json TEXT DEFAULT '[]',release_candidates_json TEXT DEFAULT '[]',evidence_json TEXT DEFAULT '[]',trace_json TEXT DEFAULT '[]',rules_json TEXT DEFAULT '[]',lyrics_json TEXT DEFAULT '{}',artwork_json TEXT DEFAULT '{}',error TEXT,expected_duration_ms INTEGER,actual_duration_ms INTEGER);
        CREATE TABLE IF NOT EXISTS library_items (id TEXT PRIMARY KEY,created_at REAL NOT NULL,updated_at REAL NOT NULL,path TEXT,size INTEGER,mtime_ns INTEGER,final_json TEXT DEFAULT '{}',overrides_json TEXT DEFAULT '{}',recording_json TEXT DEFAULT '{}',release_json TEXT DEFAULT '{}',lyrics_json TEXT DEFAULT '{}',artwork_json TEXT DEFAULT '{}',verified INTEGER DEFAULT 0,written_at REAL);
        CREATE INDEX IF NOT EXISTS idx_research_status ON research_items(status); CREATE INDEX IF NOT EXISTS idx_library_path ON library_items(path);''')

def row_to_item(r):
    d=dict(r); obj={'system','overrides','final','recording','release','lyrics','artwork'}; arr={'candidates','release_candidates','evidence','trace','rules'}
    for key in obj|arr:
        col=key+'_json'
        if col in d:
            try:d[key]=json.loads(d[col] or ('[]' if key in arr else '{}'))
            except Exception:d[key]=[] if key in arr else {}
            d.pop(col,None)
    return d

def load_items(table):
    if table not in {'research_items','library_items'}: raise ValueError('invalid table')
    with connect() as c:return [row_to_item(r) for r in c.execute(f'SELECT * FROM {table} ORDER BY created_at').fetchall()]

def find_item(item_id):
    with connect() as c:
        for table in ('research_items','library_items'):
            r=c.execute(f'SELECT * FROM {table} WHERE id=?',(item_id,)).fetchone()
            if r:return row_to_item(r),table
    return None,None

def upsert_research(item):
    item['updated_at']=time.time(); now=item.get('created_at',item['updated_at'])
    keys=['system','overrides','final','recording','release','candidates','release_candidates','evidence','trace','rules','lyrics','artwork']
    vals=[item['id'],now,item['updated_at'],item.get('path'),item.get('size'),item.get('mtime_ns'),item.get('input_artist'),item.get('input_title'),item.get('status','QUEUED'),item.get('attention',''),item.get('confidence'),int(bool(item.get('refresh'))),item.get('generation',1)]
    vals += [json.dumps(item.get(k,[] if k in {'candidates','release_candidates','evidence','trace','rules'} else {}),ensure_ascii=False) for k in keys]
    vals += [item.get('error'),item.get('expected_duration_ms'),item.get('actual_duration_ms')]
    cols='id,created_at,updated_at,path,size,mtime_ns,input_artist,input_title,status,attention,confidence,refresh,generation,'+','.join(k+'_json' for k in keys)+',error,expected_duration_ms,actual_duration_ms'
    qs=','.join('?' for _ in vals)
    with connect() as c:c.execute(f'INSERT OR REPLACE INTO research_items ({cols}) VALUES ({qs})',vals)

def insert_library(item):
    now=time.time(); keys=['final','overrides','recording','release','lyrics','artwork']
    vals=[item['id'],item.get('created_at',now),now,item.get('path'),item.get('size'),item.get('mtime_ns')]+[json.dumps(item.get(k,{}),ensure_ascii=False) for k in keys]+[1,item.get('written_at')]
    with connect() as c:c.execute('INSERT OR REPLACE INTO library_items (id,created_at,updated_at,path,size,mtime_ns,final_json,overrides_json,recording_json,release_json,lyrics_json,artwork_json,verified,written_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',vals)
def delete_research(i):
    with connect() as c:c.execute('DELETE FROM research_items WHERE id=?',(i,))
def duplicate_exists(path,identity):
    resolved=str(Path(path).resolve())
    with connect() as c: rows=c.execute('SELECT path,size,mtime_ns FROM research_items UNION ALL SELECT path,size,mtime_ns FROM library_items').fetchall()
    return any(r['path']==resolved or (r['size']==identity['size'] and r['mtime_ns']==identity['mtime_ns']) for r in rows)

def clear_id3(path):
    try: tags=ID3(path)
    except ID3NoHeaderError: tags=ID3()
    else: tags.delete(path,delete_v1=True,delete_v2=True); tags=ID3()
    return tags

def write_mp3(path,final,lyrics=None,artwork=None):
    tags=clear_id3(path)
    pairs=[(TPE1,'artist'),(TPE2,'album_artist'),(TIT2,'title'),(TALB,'album'),(TDRC,'year'),(TRCK,'track'),(TCON,'genre')]
    for cls,k in pairs:
        if final.get(k): tags.add(cls(encoding=3,text=str(final[k])))
    if final.get('country'): tags.add(TXXX(encoding=3,desc='Country',text=str(final['country'])))
    if lyrics and isinstance(lyrics,dict) and lyrics.get('original'): tags.add(USLT(encoding=3,lang='eng',desc='',text=lyrics['original']))
    if artwork and isinstance(artwork,dict) and artwork.get('path'):
        p=Path(artwork['path'])
        if p.exists(): tags.add(APIC(encoding=3,mime=artwork.get('mime','image/jpeg'),type=3,desc='Front Cover',data=p.read_bytes()))
    tags.save(path,v2_version=3)

def verify_mp3(path,final):
    vals=tag_values(path); return norm(vals.get('artist'))==norm(final.get('artist')) and norm(vals.get('title'))==norm(final.get('title'))

def search_recordings(artist,title):
    q=f'artist:"{artist}" AND recording:"{title}"'; data=http_json(f'{MB_BASE}/recording/?query={quote(q)}&fmt=json&limit=10')
    return data.get('recordings',[])

def search_releases(recording_id):
    data=http_json(f'{MB_BASE}/recording/{recording_id}?inc=release-rels+releases&fmt=json')
    out=[]
    for rel in data.get('releases',[]):
        positions=[]
        for media in rel.get('media',[]):
            for tr in media.get('tracks',[]):
                if tr.get('recording',{}).get('id')==recording_id: positions.append(tr.get('position'))
        out.append({'id':rel.get('id'),'title':rel.get('title',''),'date':rel.get('date',''),'country':(rel.get('country') or ''),'type':rel.get('release-group',{}).get('primary-type',''),'positions':positions,'status':rel.get('status',''),'artist':', '.join(a.get('name','') for a in rel.get('artist-credit',[]) if isinstance(a,dict))})
    return out

def perform_research(item):
    if item.get('generation',1)!=item.get('generation',1): return item
    artist=item.get('input_artist',''); title=item.get('input_title',''); item['status']='RESEARCHING'; upsert_research(item)
    try:
        recs=search_recordings(artist,title)
        actual=item.get('actual_duration_ms'); ranked=[]
        for r in recs:
            dur=(r.get('length') or 0); score=0
            if norm(r.get('title'))==norm(title): score+=45
            if any(norm(a.get('name'))==norm(artist) for a in r.get('artist-credit',[]) if isinstance(a,dict)): score+=35
            if actual and dur: score+=max(0,20-abs(actual-dur)/1000)
            ranked.append({'id':r.get('id'),'title':r.get('title',''),'artist':''.join(a.get('name','') for a in r.get('artist-credit',[]) if isinstance(a,dict)),'duration_ms':dur,'score':round(score,1),'disambiguation':r.get('disambiguation','')})
        ranked.sort(key=lambda x:x['score'],reverse=True); item['candidates']=ranked
        if not ranked: item['status']='NEEDS ATTENTION'; item['attention']='NO RECORDING CANDIDATE FOUND'; upsert_research(item); return item
        best=ranked[0]; item['recording']=best; item['confidence']=min(99,round(best['score']))
        item['expected_duration_ms']=best.get('duration_ms');
        if actual and best.get('duration_ms'):
            item['actual_duration_ms']=actual
            if abs(actual-best['duration_ms'])>12000: item['attention']='POSSIBLE RECORDING MISMATCH'
        item['release_candidates']=search_releases(best['id'])[:30]
        if item['release_candidates']:
            rel=next((r for r in item['release_candidates'] if r.get('positions')),item['release_candidates'][0]); item['release']=rel
            item['system']={'artist':artist,'album_artist':artist,'title':title,'album':rel.get('title',''),'year':(rel.get('date') or '')[:4],'track':str(rel.get('positions',[1])[0] if rel.get('positions') else 1),'country':rel.get('country',''),'release_type':rel.get('type','')}
        else: item['system']={'artist':artist,'album_artist':artist,'title':title}
        item['final']={**item['system'],**item.get('overrides',{})}
        item['evidence']=[{'source':'MusicBrainz','kind':'Recording','summary':f"{best.get('artist')} — {best.get('title')} · score {best.get('score')}",'url':f"https://musicbrainz.org/recording/{best.get('id')}"}]
        item['trace']=[f"Recording selected from {len(recs)} MusicBrainz candidates.",f"Release candidates checked: {len(item['release_candidates'])}."]
        item['status']='NEEDS ATTENTION' if item.get('attention') else 'READY'
    except Exception as exc: item['status']='FAILED'; item['error']=str(exc)
    upsert_research(item); return item

class ResearchQueue:
    def __init__(self,app): self.app=app; self.pool=ThreadPoolExecutor(max_workers=MAX_WORKERS); self.pending=[]; self.active=0; self.paused=False
    def enqueue(self,rid):
        if rid not in self.pending:self.pending.append(rid)
        self.pump()
    def pump(self):
        while not self.paused and self.active<MAX_WORKERS and self.pending:
            rid=self.pending.pop(0); item,_=find_item(rid)
            if not item: continue
            self.active+=1; fut=self.pool.submit(perform_research,item); fut.add_done_callback(lambda f,r=rid:self.done(r,f))
    def done(self,rid,fut):
        self.active-=1
        try: result=fut.result()
        except Exception as exc: result=None
        self.app.after(0,lambda:self.app.refresh_ui())
        self.pump()
    def pause(self): self.paused=True
    def resume(self): self.paused=False; self.pump()

class App(tk.Tk):
    def __init__(self):
        super().__init__(); self.title(f'{APP_NAME} — {APP_VERSION}'); self.geometry('1200x760'); self.queue=ResearchQueue(self); self.current_id=None
        style=ttk.Style(self); style.configure('Title.TLabel',font=('Segoe UI',20,'bold')); style.configure('Section.TLabel',font=('Segoe UI',12,'bold')); style.configure('Status.TLabel',foreground='#555')
        self.columnconfigure(1,weight=1); self.rowconfigure(0,weight=1); self.nav=ttk.Frame(self,padding=12); self.nav.grid(row=0,column=0,sticky='ns'); self.body=ttk.Frame(self,padding=18); self.body.grid(row=0,column=1,sticky='nsew'); self._nav(); self.show_home()
    def _nav(self):
        for text,cmd in [('HOME',self.show_home),('MUSIC IN PROGRESS',self.show_progress),('LIBRARY',self.show_library),('SEARCH',self.show_search)]: ttk.Button(self.nav,text=text,command=cmd,width=22).pack(pady=4)
    def clear(self):
        for w in self.body.winfo_children(): w.destroy()
    def _tree(self,parent,cols,widths):
        t=ttk.Treeview(parent,columns=cols,show='headings'); t.pack(fill='both',expand=True)
        for c,w in zip(cols,widths): t.heading(c,text=c); t.column(c,width=w,anchor='w')
        return t
    def refresh_ui(self):
        if self.current_id: self.open_item(self.current_id)
        else:self.show_progress()
    def show_home(self):
        self.current_id=None; self.clear(); ttk.Label(self.body,text='START RESEARCH',style='Title.TLabel').pack(anchor='w',pady=(0,12)); f=ttk.Frame(self.body); f.pack(fill='x')
        ttk.Button(f,text='ADD MP3',command=self.add_mp3).pack(side='left',padx=4); ttk.Button(f,text='ADD FOLDER',command=self.add_folder).pack(side='left',padx=4); ttk.Button(f,text='REFRESH LIBRARY',command=self.show_library).pack(side='left',padx=4)
        items=load_items('research_items'); lib=load_items('library_items'); ttk.Label(self.body,text=f'{len(items)} Music in Progress · {len(lib)} Library Items',style='Status.TLabel').pack(anchor='w',pady=16); ttk.Button(self.body,text='OPEN MUSIC IN PROGRESS',command=self.show_progress).pack(anchor='w')
    def add_mp3(self):
        p=filedialog.askopenfilename(filetypes=[('MP3 files','*.mp3')]);
        if p:self.create_research(p)
    def add_folder(self):
        d=filedialog.askdirectory();
        if not d:return
        for p in sorted(Path(d).rglob('*.mp3')): self.create_research(str(p),silent=True)
        self.show_progress()
    def create_research(self,path,silent=False):
        try: ident=file_identity(path); tags=tag_values(path); a,t=parse_filename(path); a=tags.get('artist',a); t=tags.get('title',t); dur=audio_duration_ms(path)
        except Exception as exc:
            if not silent:messagebox.showerror('Cannot read MP3',str(exc)); return
        if duplicate_exists(path,ident):
            if not silent:messagebox.showinfo('Existing research','This MP3 is already known to Music Metadata Researcher.')
            return
        item={'id':str(uuid.uuid4()),'created_at':time.time(),'updated_at':time.time(),'path':ident['path'],'size':ident['size'],'mtime_ns':ident['mtime_ns'],'input_artist':a,'input_title':t,'status':'QUEUED','confidence':None,'generation':1,'actual_duration_ms':dur,'overrides':{},'system':{},'final':{},'recording':{},'release':{},'candidates':[],'release_candidates':[],'evidence':[],'trace':[],'rules':[],'lyrics':{},'artwork':extract_front_cover(path) or {}}
        upsert_research(item); self.queue.enqueue(item['id'])
        if not silent:self.open_item(item['id'])
    def show_progress(self):
        self.current_id=None; self.clear(); ttk.Label(self.body,text='MUSIC IN PROGRESS',style='Title.TLabel').pack(anchor='w'); t=self._tree(self.body,['Status','Artist','Title','Confidence','MP3'],[160,220,260,100,430]);
        for i in load_items('research_items'):
            f=i.get('final',{}); t.insert('', 'end',iid=i['id'],values=(i.get('status'),f.get('artist') or i.get('input_artist'),f.get('title') or i.get('input_title'),i.get('confidence') or '—',i.get('path')))
        t.bind('<Double-1>',lambda e:self.open_item(t.focus())); ttk.Button(self.body,text='PAUSE RESEARCH',command=self.queue.pause).pack(side='left',pady=8); ttk.Button(self.body,text='RESUME RESEARCH',command=self.queue.resume).pack(side='left',pady=8,padx=6)
    def open_item(self,rid):
        item,table=find_item(rid)
        if not item:return
        self.current_id=rid; self.clear(); ttk.Button(self.body,text='← BACK',command=self.show_progress).pack(anchor='w'); ttk.Label(self.body,text='RESEARCH ITEM',style='Title.TLabel').pack(anchor='w',pady=8)
        f=item.get('final',{}); ttk.Label(self.body,text=f"{f.get('artist') or item.get('input_artist','')} — {f.get('title') or item.get('input_title','')}",style='Section.TLabel').pack(anchor='w'); ttk.Label(self.body,text=f"Status: {item.get('status')} · Confidence: {item.get('confidence') or '—'}",style='Status.TLabel').pack(anchor='w',pady=4)
        nb=ttk.Notebook(self.body); nb.pack(fill='both',expand=True,pady=10)
        meta=ttk.Frame(nb,padding=10); rel=ttk.Frame(nb,padding=10); ev=ttk.Frame(nb,padding=10); nb.add(meta,text='FINAL METADATA'); nb.add(rel,text='RELEASE / RECORDING'); nb.add(ev,text='EVIDENCE')
        self._metadata(meta,item); self._release(rel,item); self._evidence(ev,item)
    def _metadata(self,parent,item):
        fields=['artist','album_artist','title','album','year','track','genre','country']; vars={}
        for r,k in enumerate(fields): ttk.Label(parent,text=k.title()).grid(row=r,column=0,sticky='w',padx=4,pady=3); v=tk.StringVar(value=item.get('final',{}).get(k,'')); vars[k]=v; ttk.Entry(parent,textvariable=v,width=70).grid(row=r,column=1,sticky='ew',padx=4,pady=3)
        ttk.Label(parent,text='Lyrics (original)').grid(row=len(fields),column=0,sticky='nw'); lyric=tk.Text(parent,height=8,width=70); lyric.grid(row=len(fields),column=1,sticky='nsew'); lyric.insert('1.0',item.get('lyrics',{}).get('original','')); parent.columnconfigure(1,weight=1)
        def save():
            for k,v in vars.items():
                val=v.get().strip(); sys=item.get('system',{}).get(k,'')
                if val!=sys:item.setdefault('overrides',{})[k]=val
                else:item.setdefault('overrides',{}).pop(k,None)
            item['final']={**item.get('system',{}),**item.get('overrides',{})}; item.setdefault('lyrics',{})['original']=lyric.get('1.0','end-1c'); upsert_research(item); self.open_item(item['id'])
        def write():
            save(); item2,_=find_item(item['id']); p=Path(item2['path'])
            try:
                before=file_identity(p); write_mp3(p,item2['final'],item2.get('lyrics'),item2.get('artwork')); 
                if not verify_mp3(p,item2['final']):raise RuntimeError('VERIFY FAILED — Artist/Title do not match Final Metadata')
                target=p.with_name(f"{item2['final'].get('artist','Unknown')} - {item2['final'].get('title','Unknown')}.mp3".replace('/','_').replace('\\','_'))
                if target!=p and target.exists():target=p.with_name(p.stem+' [MMR].mp3')
                if target!=p:os.replace(p,target)
                ident=file_identity(target); item2.update({'path':str(target),'size':ident['size'],'mtime_ns':ident['mtime_ns'],'written_at':time.time()}); insert_library(item2); delete_research(item2['id']); messagebox.showinfo('WRITE COMPLETE','MP3 written, verified, renamed, and added to Library.'); self.show_library()
            except Exception as exc:messagebox.showerror('WRITE FAILED',str(exc))
        b=ttk.Frame(parent); b.grid(row=len(fields)+1,column=1,sticky='e',pady=8); ttk.Button(b,text='SAVE EDITS',command=save).pack(side='left',padx=4); ttk.Button(b,text='RESEARCH AGAIN',command=lambda:self.queue.enqueue(item['id'])).pack(side='left',padx=4); ttk.Button(b,text='✎ WRITE',command=write).pack(side='left',padx=4)
    def _release(self,parent,item):
        c=item.get('recording',{}); ttk.Label(parent,text='CURRENT RECORDING',style='Section.TLabel').pack(anchor='w'); ttk.Label(parent,text=f"{c.get('artist','')} — {c.get('title','')} · {c.get('duration_ms') or '—'} ms").pack(anchor='w',pady=4); ttk.Label(parent,text='RELEASE CANDIDATES',style='Section.TLabel').pack(anchor='w',pady=(12,4))
        for rel in item.get('release_candidates',[]):
            f=ttk.Frame(parent,relief='ridge',padding=6); f.pack(fill='x',pady=2); ttk.Label(f,text=f"{rel.get('title')} · {rel.get('date','')} · {rel.get('type','')} · track {rel.get('positions') or '—'}").pack(side='left');
            if rel.get('positions'):ttk.Button(f,text='USE THIS RELEASE',command=lambda rr=rel:self.use_release(item['id'],rr)).pack(side='right')
    def use_release(self,rid,rel):
        item,_=find_item(rid); item['release']=rel; item['system'].update({'album':rel.get('title',''),'year':(rel.get('date') or '')[:4],'track':str(rel.get('positions',[1])[0] if rel.get('positions') else 1),'country':rel.get('country',''),'release_type':rel.get('type','')}); item['final']={**item['system'],**item.get('overrides',{})}; item['generation']=int(item.get('generation',1))+1; item['trace']=item.get('trace',[])+[f"User selected Release: {rel.get('title','')}"]; upsert_research(item); self.open_item(rid)
    def _evidence(self,parent,item):
        ttk.Label(parent,text='DECISION TRACE',style='Section.TLabel').pack(anchor='w');
        for x in item.get('trace',[]):ttk.Label(parent,text='• '+x).pack(anchor='w')
        ttk.Label(parent,text='RULES OF RON',style='Section.TLabel').pack(anchor='w',pady=(12,4));
        for x in item.get('rules',[]):ttk.Label(parent,text='• '+x).pack(anchor='w')
        ttk.Label(parent,text='EVIDENCE',style='Section.TLabel').pack(anchor='w',pady=(12,4))
        for ev in item.get('evidence',[]):
            f=ttk.Frame(parent,relief='ridge',padding=6); f.pack(fill='x',pady=2); ttk.Label(f,text=f"{ev.get('source')} · {ev.get('kind')} · {ev.get('summary','')}").pack(side='left');
            if ev.get('url'):ttk.Button(f,text='OPEN SOURCE',command=lambda u=ev['url']:webbrowser.open(u)).pack(side='right')
    def show_library(self):
        self.current_id=None; self.clear(); ttk.Label(self.body,text='LIBRARY',style='Title.TLabel').pack(anchor='w'); t=self._tree(self.body,['Artist','Title','Album','Year','MP3'],[210,260,250,90,430])
        for r in load_items('library_items'):
            f=r.get('final',{}); t.insert('','end',iid=r['id'],values=(f.get('artist',''),f.get('title',''),f.get('album',''),f.get('year',''),r.get('path','')))
        t.bind('<Double-1>',lambda e:self.open_library(t.focus()))
    def open_library(self,rid):
        item,_=find_item(rid); self.current_id=rid; self.clear(); ttk.Button(self.body,text='← BACK',command=self.show_library).pack(anchor='w'); ttk.Label(self.body,text='LIBRARY ITEM',style='Title.TLabel').pack(anchor='w',pady=8); f=item.get('final',{})
        for k in ['artist','album_artist','title','album','year','track','genre','country']:ttk.Label(self.body,text=f'{k.title()}: {f.get(k,"-")}').pack(anchor='w',pady=2)
        ttk.Label(self.body,text='MP3: '+item.get('path',''),style='Status.TLabel').pack(anchor='w',pady=8); ttk.Button(self.body,text='REFRESH',command=lambda:self.refresh_library(item)).pack(anchor='w')
    def refresh_library(self,item):
        try:ident=file_identity(item['path'])
        except Exception:messagebox.showwarning('MP3 unavailable','MP3 NOT CURRENTLY AVAILABLE'); return
        item.update({'size':ident['size'],'mtime_ns':ident['mtime_ns'],'refresh':True,'status':'QUEUED','attention':'','error':'','generation':int(item.get('generation',1))+1});
        with connect() as c:c.execute('DELETE FROM library_items WHERE id=?',(item['id'],))
        upsert_research(item); self.queue.enqueue(item['id']); self.show_progress()
    def show_search(self):
        self.current_id=None; self.clear(); ttk.Label(self.body,text='SEARCH',style='Title.TLabel').pack(anchor='w'); q=tk.StringVar(); ttk.Entry(self.body,textvariable=q,width=70).pack(anchor='w',pady=8); out=tk.Text(self.body); out.pack(fill='both',expand=True)
        def go():
            term=norm(q.get()); out.delete('1.0','end')
            if not term:return
            for table in ('research_items','library_items'):
                for item in load_items(table):
                    f=item.get('final',{}); ly=item.get('lyrics',{}); hay=' '.join(str(x or '') for x in [f.get('artist'),f.get('title'),f.get('album'),item.get('input_artist'),item.get('input_title'),ly.get('original')]);
                    if term in norm(hay):out.insert('end',f"{table} · {f.get('artist','')} — {f.get('title','')} · {f.get('album','')}\n")
        ttk.Button(self.body,text='SEARCH',command=go).pack(anchor='w',pady=6)

if __name__=='__main__': init_db(); App().mainloop()
