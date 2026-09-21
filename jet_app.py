from __future__ import annotations
import csv, gzip, hashlib, hmac, io, json, math, os, re, sqlite3, time, uuid, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterable, Protocol
from contextlib import contextmanager
from urllib.parse import urlencode, urlparse, quote
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parent
DB=ROOT/'jet.db'
VERSION='9.0.0'
ENGINE_NAME='Jet Tesfa Universal AI Opportunity Engine — Hardened Candidate'
BRAND='Jet Tesfa'
EXPERIENCE_VERSION='9.0.0'
ARENA_MIN_OBS=100
WEIGHTS={'quality':.20,'trust':.18,'fit':.24,'deal':.14,'freshness':.08,'price_confidence':.08,'satisfaction':.05,'commission':.03}
CATEGORIES={
 'tv':['טלוויז','מסך','television',' tv ','qled','oled'], 'phone':['טלפון','סמארטפון','iphone','galaxy','phone'],
 'laptop':['מחשב','לפטופ','laptop','macbook'], 'coffee':['קפה','coffee','espresso'], 'travel':['מלון','טיסה','חופשה','hotel','flight','travel'],
 'home':['בית','מטבח','שואב','vacuum','home'], 'fashion':['בגד','נעל','fashion','shoe'], 'gadget':['גאדגט','gadget','אוזניות','headphone']}


_RATE_LOCK=threading.Lock()
_RATE={}
def client_ip(environ):
    x=(environ.get('HTTP_X_FORWARDED_FOR') or environ.get('REMOTE_ADDR') or 'unknown').split(',')[0].strip()
    return x[:80]
def rate_allowed(key,limit=90,window=60):
    t=now()
    with _RATE_LOCK:
        arr=[x for x in _RATE.get(key,[]) if t-x<window]
        if len(arr)>=limit: _RATE[key]=arr; return False
        arr.append(t); _RATE[key]=arr
        if len(_RATE)>5000:
            for k in list(_RATE)[:1000]: _RATE.pop(k,None)
        return True
def security_headers():
    return [('X-Content-Type-Options','nosniff'),('X-Frame-Options','SAMEORIGIN'),('Referrer-Policy','strict-origin-when-cross-origin'),('Permissions-Policy','camera=(), microphone=(), geolocation=()'),('Content-Security-Policy',"default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self' https:")]
def valid_session(s):
    return re.sub(r'[^A-Za-z0-9_.:-]','',str(s or 'guest'))[:128] or 'guest'
def consent_mode(environ):
    return (environ.get('HTTP_X_JET_CONSENT') or 'essential').lower()[:24]
def readiness():
    try:
        init_db()
        with db_conn() as c: c.execute('SELECT 1').fetchone()
        if env('JET_ENV').lower() == 'production':
            token=env('JET_ADMIN_TOKEN')
            if len(token) < 32 or token == 'CHANGE_TO_A_LONG_RANDOM_SECRET': return False,'secure_admin_token_required'
            if env('JET_DEBUG') == '1': return False,'debug_must_be_disabled'
            if env('JET_DEMO','0') == '1': return False,'demo_must_be_disabled'
            if not any(c.enabled() for c in CONNECTORS if c.name != 'demo'): return False,'live_source_required'
        return True,''
    except Exception as e:return False,str(e)[:160]


@contextmanager
def db_conn():
    c=sqlite3.connect(DB, timeout=15)
    try:
        yield c
        c.commit()
    except:
        c.rollback()
        raise
    finally:
        c.close()

def env(name,default=''): return os.getenv(name,default).strip()
def clamp(v): return max(0.,min(1.,float(v)))
def now(): return time.time()
def safe_float(v,default=0):
    try:return float(str(v).replace(',','').replace('₪','').strip())
    except:return default

def http_json(url,headers=None,timeout=12):
    req=Request(url,headers={'User-Agent':'JetTesfa/8.0',**(headers or {})})
    with urlopen(req,timeout=timeout) as r:return json.loads(r.read().decode('utf-8'))
def http_bytes(url,headers=None,timeout=20):
    req=Request(url,headers={'User-Agent':'JetTesfa/8.0',**(headers or {})})
    with urlopen(req,timeout=timeout) as r:return r.read(),dict(r.headers)

@dataclass
class Opportunity:
    source:str; product_id:str; title:str; url:str; price:float; currency:str='ILS'; category:str='general'; merchant:str=''
    quality:float=.6; trust:float=.6; fit:float=.5; deal:float=.5; commission:float=0; freshness:float=.6; price_confidence:float=.6
    satisfaction:float=.6; old_price:float|None=None; image:str=''; affiliate:bool=False; shipping:float|None=None; available:bool=True
    gtin:str=''; brand:str=''; description:str=''; updated_at:float=field(default_factory=now); metadata:dict=field(default_factory=dict)

class Connector(Protocol):
    name:str
    def enabled(self)->bool: ...
    def search(self,intent:dict)->Iterable[Opportunity]: ...

class DemoConnector:
    name='demo'
    CATALOG=[
      Opportunity('demo','tv75q','75″ QLED 4K Smart TV','https://example.com/tv-qled',4490,category='tv',merchant='Demo Store',quality=.96,trust=.92,deal=.78,freshness=.95,price_confidence=.90,old_price=5290,image='https://images.unsplash.com/photo-1593359677879-a4bb92f829d1?auto=format&fit=crop&w=900&q=70'),
      Opportunity('demo','tv75v','75″ 4K Value Smart TV','https://example.com/tv-value',3490,category='tv',merchant='Demo Store',quality=.82,trust=.88,deal=.88,freshness=.93,price_confidence=.90,old_price=4290),
      Opportunity('demo','phone1','Flagship Smartphone 256GB','https://example.com/phone',3190,category='phone',merchant='Demo Mobile',quality=.94,trust=.94,deal=.66,freshness=.92,price_confidence=.91,old_price=3590),
      Opportunity('demo','laptop1','14″ AI Laptop 32GB / 1TB','https://example.com/laptop',4890,category='laptop',merchant='Demo Tech',quality=.93,trust=.91,deal=.72,freshness=.90,price_confidence=.88,old_price=5590),
      Opportunity('demo','coffee1','Premium Coffee Machine','https://example.com/coffee',1290,category='coffee',merchant='Demo Home',quality=.89,trust=.90,deal=.80,freshness=.89,price_confidence=.87,old_price=1690)]
    def enabled(self): return env('JET_DEMO','1')=='1'
    def search(self,intent):
        q=intent.get('query','').lower(); cat=intent.get('category'); budget=intent.get('max_price'); terms=set(re.findall(r'[\w\u0590-\u05ff]+',q))
        for base in self.CATALOG:
            text=(base.title+' '+base.category).lower(); fit=.42
            if cat and base.category==cat: fit=.96
            elif any(t in text for t in terms if len(t)>2): fit=.82
            if budget and base.price<=budget: fit=min(1,fit+.04)
            o=Opportunity(**asdict(base)); o.fit=fit; yield o

class EbayConnector:
    name='ebay'
    _token=''
    _token_until=0
    def enabled(self): return bool(env('EBAY_CLIENT_ID') and env('EBAY_CLIENT_SECRET'))
    def token(self):
        import base64
        if self._token and now() < self._token_until: return self._token
        creds=base64.b64encode(f"{env('EBAY_CLIENT_ID')}:{env('EBAY_CLIENT_SECRET')}".encode()).decode()
        req=Request('https://api.ebay.com/identity/v1/oauth2/token',data=urlencode({'grant_type':'client_credentials','scope':'https://api.ebay.com/oauth/api_scope'}).encode(),headers={'Authorization':'Basic '+creds,'Content-Type':'application/x-www-form-urlencoded'})
        with urlopen(req,timeout=12) as r:
            d=json.loads(r.read()); self._token=d['access_token']; self._token_until=now()+max(60,int(d.get('expires_in',7200))-120); return self._token
    def search(self,intent):
        token=self.token(); params={'q':intent['query'],'limit':'30'}
        if intent.get('max_price'): params['filter']=f"price:[..{intent['max_price']}],priceCurrency:USD"
        headers={'Authorization':'Bearer '+token,'X-EBAY-C-MARKETPLACE-ID':env('EBAY_MARKETPLACE','EBAY_US')}
        camp=env('EBAY_CAMPAIGN_ID')
        if camp: headers['X-EBAY-C-ENDUSERCTX']=f'affiliateCampaignId={camp}'
        data=http_json('https://api.ebay.com/buy/browse/v1/item_summary/search?'+urlencode(params),headers)
        for x in data.get('itemSummaries',[]):
            p=safe_float((x.get('price') or {}).get('value')); old=safe_float((x.get('marketingPrice') or {}).get('originalPrice',{}).get('value')) or None
            url=x.get('itemAffiliateWebUrl') or x.get('itemWebUrl') or ''
            if not p or not url: continue
            yield Opportunity('ebay',x.get('itemId',''),x.get('title',''),url,p,(x.get('price') or {}).get('currency','USD'),intent.get('category') or 'general','eBay',.78,.92,.65,.6,.04,.94,.86,.75,old,(x.get('image') or {}).get('imageUrl',''),bool(x.get('itemAffiliateWebUrl')),description=x.get('shortDescription',''),metadata={'condition':x.get('condition','')})

class AwinFeedConnector:
    name='awin'
    def enabled(self): return bool(env('AWIN_FEED_URL'))
    def search(self,intent):
        raw,h=http_bytes(env('AWIN_FEED_URL'))
        if env('AWIN_FEED_URL').endswith('.gz') or raw[:2]==b'\x1f\x8b': raw=gzip.decompress(raw)
        text=raw.decode('utf-8-sig',errors='replace'); reader=csv.DictReader(io.StringIO(text))
        terms=[t for t in re.findall(r'[\w\u0590-\u05ff]+',intent['query'].lower()) if len(t)>2]; n=0
        for x in reader:
            title=x.get('product_name') or x.get('name') or ''; desc=x.get('description') or ''; hay=(title+' '+desc+' '+(x.get('merchant_category') or '')).lower()
            if terms and not any(t in hay for t in terms): continue
            p=safe_float(x.get('search_price') or x.get('store_price') or x.get('price')); url=x.get('aw_deep_link') or x.get('merchant_deep_link') or ''
            if not p or not url: continue
            old=safe_float(x.get('rrp_price') or x.get('product_price_old')) or None
            discount=clamp((old-p)/old) if old and old>p else clamp(safe_float(x.get('savings_percent'))/100)
            yield Opportunity('awin',x.get('aw_product_id') or x.get('merchant_product_id') or hashlib.sha1(url.encode()).hexdigest()[:16],title,url,p,x.get('currency') or 'GBP',intent.get('category') or x.get('merchant_category') or 'general',x.get('merchant_name') or 'Awin merchant',.72,.90,.7,max(.45,discount),.06,.9,.86,.7,old,x.get('aw_image_url') or x.get('merchant_image_url') or '',True,gtin=x.get('product_GTIN') or x.get('ean') or '',brand=x.get('brand_name') or '',description=desc,metadata={'stock':x.get('stock_status') or x.get('in_stock')})
            n+=1
            if n>=80:return

CONNECTORS=[EbayConnector(),AwinFeedConnector(),DemoConnector()]

def parse_intent(text:str)->dict:
    t=' '.join(text.strip().split()); low=t.lower(); intent={'query':t,'category':None,'max_price':None,'min_price':None,'preferences':[]}
    padded=' '+low+' '
    for c,keys in CATEGORIES.items():
        if any(k in padded for k in keys): intent['category']=c; break
    nums=[]
    for raw in re.findall(r'(?<!\d)(\d{1,3}(?:[,.]\s?\d{3})+|\d{3,7})(?!\d)',low):
        try: nums.append(float(re.sub(r'[,\s]','',raw)))
        except:pass
    if nums: intent['max_price']=max(nums)
    if any(w in low for w in ['איכות','quality','premium']):intent['preferences'].append('quality')
    if any(w in low for w in ['זול','חסכוני','value','cheap']):intent['preferences'].append('value')
    if any(w in low for w in ['מהיר','משלוח','delivery']):intent['preferences'].append('delivery')
    return intent

def init_db():
    with db_conn() as c:
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,ts REAL,session_id TEXT,event TEXT,product_id TEXT,payload TEXT);
        CREATE TABLE IF NOT EXISTS dna(session_id TEXT PRIMARY KEY,profile TEXT,updated REAL);
        CREATE TABLE IF NOT EXISTS products(key TEXT PRIMARY KEY,source TEXT,product_id TEXT,title TEXT,url TEXT,price REAL,currency TEXT,merchant TEXT,category TEXT,jet_score REAL,payload TEXT,seen REAL);
        CREATE TABLE IF NOT EXISTS price_history(id INTEGER PRIMARY KEY AUTOINCREMENT,product_key TEXT,ts REAL,price REAL,currency TEXT);
        CREATE TABLE IF NOT EXISTS feedback(id TEXT PRIMARY KEY,ts REAL,session_id TEXT,product_id TEXT,rating REAL,purchased INTEGER,note TEXT);
        CREATE TABLE IF NOT EXISTS experiments(name TEXT PRIMARY KEY,champion TEXT,challenger TEXT,metrics TEXT,updated REAL);
        CREATE TABLE IF NOT EXISTS alerts(id TEXT PRIMARY KEY,session_id TEXT,query TEXT,target_price REAL,active INTEGER,created REAL,last_checked REAL);
        CREATE TABLE IF NOT EXISTS model_stats(model TEXT PRIMARY KEY,impressions INTEGER,clicks INTEGER,purchases INTEGER,refunds INTEGER,satisfaction_sum REAL,satisfaction_n INTEGER,updated REAL);
        CREATE TABLE IF NOT EXISTS source_health(source TEXT PRIMARY KEY,ok INTEGER,latency_ms REAL,result_count INTEGER,last_error TEXT,updated REAL);
        ''')

def log_event(session,event,product_id='',payload=None):
    init_db()
    with db_conn() as c:c.execute('INSERT INTO events VALUES(?,?,?,?,?,?)',(str(uuid.uuid4()),now(),session,event,product_id,json.dumps(payload or {},ensure_ascii=False)))
def get_dna(session):
    init_db()
    with db_conn() as c:
        r=c.execute('SELECT profile FROM dna WHERE session_id=?',(session,)).fetchone(); return json.loads(r[0]) if r else {'categories':{},'queries':0,'clicks':{},'budget_sum':0,'budget_n':0}
def update_dna(session,intent=None,event=None,product=None):
    p=get_dna(session)
    if intent:
        p['queries']=p.get('queries',0)+1; cat=intent.get('category')
        if cat:p['categories'][cat]=p['categories'].get(cat,0)+1
        if intent.get('max_price'):p['budget_sum']=p.get('budget_sum',0)+intent['max_price'];p['budget_n']=p.get('budget_n',0)+1
    if event=='outbound_click' and product:p['clicks'][product]=p['clicks'].get(product,0)+1
    with db_conn() as c:c.execute('INSERT INTO dna VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET profile=excluded.profile,updated=excluded.updated',(session,json.dumps(p,ensure_ascii=False),now()))

def canonical_key(o):
    if o.gtin:return 'gtin:'+re.sub(r'\D','',o.gtin)
    norm=re.sub(r'\W+',' ',(o.brand+' '+o.title).lower()).strip(); return hashlib.sha1(norm.encode()).hexdigest()
def historical_deal(o):
    k=canonical_key(o); init_db()
    with db_conn() as c:
        rows=c.execute('SELECT price FROM price_history WHERE product_key=? ORDER BY ts DESC LIMIT 90',(k,)).fetchall()
    prices=[r[0] for r in rows if r[0]>0]
    if not prices:return o.deal
    med=sorted(prices)[len(prices)//2]; return clamp(.5+(med-o.price)/max(med,1)*2)
def personalize(o,dna):
    cat_hits=dna.get('categories',{}).get(o.category,0); total=max(1,sum(dna.get('categories',{}).values()))
    boost=min(.12,.12*cat_hits/total)
    if dna.get('budget_n'):
        b=dna['budget_sum']/dna['budget_n']; boost+=.05 if o.price<=b else -.04
    return boost

def total_cost(o):
    """Comparable landed cost. Taxes can be supplied by connectors in metadata."""
    tax=safe_float((o.metadata or {}).get('tax',0)); shipping=safe_float(o.shipping,0)
    return round(max(0,o.price)+max(0,shipping)+max(0,tax),2)

def seller_trust(o):
    m=o.metadata or {}; rating=safe_float(m.get('seller_rating',0)); returns=m.get('returns')
    t=clamp(o.trust)
    if rating: t=.65*t+.35*clamp(rating/100)
    if returns is False: t*=.90
    if not o.available: t*=.35
    return clamp(t)

def price_truth(o):
    """Rewards verified savings and penalizes suspicious 'discounts'."""
    hist=historical_deal(o); old=o.old_price
    advertised=clamp((old-o.price)/old) if old and old>0 and old>o.price else 0
    if old and old<=o.price: advertised=0
    return clamp(.70*hist+.30*(.5+advertised))

def timing_score(o):
    m=o.metadata or {}; stock=str(m.get('stock','')).lower(); t=clamp(o.freshness)
    if stock in {'out of stock','false','0','unavailable'} or not o.available:return .05
    if o.old_price and o.old_price>o.price:t=min(1,t+.08)
    return t

def confidence_score(o):
    fields=[bool(o.title),o.price>0,bool(o.url),bool(o.merchant),bool(o.currency)]
    completeness=sum(fields)/len(fields)
    return clamp(.50*o.price_confidence+.25*seller_trust(o)+.25*completeness)

def need_fit(o,intent,dna):
    f=clamp(o.fit+personalize(o,dna))
    budget=intent.get('max_price')
    if budget and total_cost(o)>budget:
        f*=max(.08,1-(total_cost(o)-budget)/max(budget,1))
    return clamp(f)

def score_components(o,intent,dna):
    return {
      'need_fit':need_fit(o,intent,dna),'deal_truth':price_truth(o),'seller_trust':seller_trust(o),
      'quality':clamp(o.quality),'timing':timing_score(o),'confidence':confidence_score(o),
      'satisfaction':clamp(o.satisfaction),'revenue':clamp(o.commission)}

def rank_truth(o,intent,dna):
    c=score_components(o,intent,dna)
    # Customer value dominates. Revenue is intentionally capped at 1%.
    w={'need_fit':.27,'deal_truth':.17,'seller_trust':.18,'quality':.14,'timing':.08,'confidence':.10,'satisfaction':.05,'revenue':.01}
    return round(100*clamp(sum(w[k]*c[k] for k in w)),2)

def rank_conservative(o,intent,dna):
    c=score_components(o,intent,dna)
    # Challenger: stronger downside control for trust/confidence.
    base=.29*c['need_fit']+.18*c['deal_truth']+.21*c['seller_trust']+.10*c['quality']+.07*c['timing']+.11*c['confidence']+.04*c['satisfaction']
    risk=(1-c['seller_trust'])*.08+(1-c['confidence'])*.06
    return round(100*clamp(base-risk),2)

def model_stats():
    init_db()
    with db_conn() as c:
        rows=c.execute('SELECT model,impressions,clicks,purchases,refunds,satisfaction_sum,satisfaction_n FROM model_stats').fetchall()
    return {r[0]:{'impressions':r[1],'clicks':r[2],'purchases':r[3],'refunds':r[4],'satisfaction_sum':r[5],'satisfaction_n':r[6]} for r in rows}

def model_utility(x):
    imp=max(1,x.get('impressions',0)); sat=x.get('satisfaction_sum',0)/max(1,x.get('satisfaction_n',0))
    return .30*(x.get('clicks',0)/imp)+.45*(x.get('purchases',0)/imp)-.35*(x.get('refunds',0)/imp)+.25*(sat/5)

def arena_model():
    stats=model_stats(); champ='truth_v6'; challenger='conservative_v6'
    a=stats.get(champ,{}); b=stats.get(challenger,{})
    # No automatic promotion without enough real observations.
    if min(a.get('impressions',0),b.get('impressions',0))>=ARENA_MIN_OBS and model_utility(b)>model_utility(a)*1.03:
        return challenger
    return champ

def arena_score(o,intent,dna,model=None):
    model=model or arena_model()
    return rank_conservative(o,intent,dna) if model=='conservative_v6' else rank_truth(o,intent,dna)

def decision(o,intent,dna):
    c=score_components(o,intent,dna); sc=rank_truth(o,intent,dna); budget=intent.get('max_price')
    if not o.available or c['seller_trust']<.42 or c['confidence']<.40:return 'AVOID'
    if budget and total_cost(o)>budget*1.08:return 'ALTERNATIVE'
    if c['deal_truth']>=.72 and sc>=72:return 'BUY'
    if c['deal_truth']<.48 and c['need_fit']>=.70:return 'WAIT'
    return 'TRACK'

def alternatives_hint(intent):
    # Need-first reasoning: suggests solution classes, not invented products.
    cat=intent.get('category'); hints={
      'tv':['טלוויזיה מאותה קטגוריית גודל','דגם משנה קודמת עם תמורה גבוהה'],
      'laptop':['מחשב עם פחות אחסון והרחבה עתידית','דגם עסקי מחודש ממוכר אמין'],
      'phone':['דגם משנה קודמת','נפח אחסון קטן יותר עם ענן'],
      'coffee':['מכונה פשוטה יותר + מטחנה איכותית','פתרון ידני אם השימוש נמוך']}
    return hints.get(cat,['חלופה זולה יותר שממלאת את אותו צורך','פתרון שונה לאותה מטרה'])

def record_source_health(source,ok,latency_ms,count=0,error=''):
    init_db()
    with db_conn() as c:c.execute('INSERT INTO source_health VALUES(?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET ok=excluded.ok,latency_ms=excluded.latency_ms,result_count=excluded.result_count,last_error=excluded.last_error,updated=excluded.updated',(source,1 if ok else 0,latency_ms,count,error[:300],now()))

def source_health():
    init_db()
    with db_conn() as c: rows=c.execute('SELECT source,ok,latency_ms,result_count,last_error,updated FROM source_health ORDER BY source').fetchall()
    return [{'source':r[0],'ok':bool(r[1]),'latency_ms':r[2],'result_count':r[3],'last_error':r[4],'updated':r[5]} for r in rows]

def create_alert(session,query,target_price=0):
    init_db(); aid=str(uuid.uuid4())
    with db_conn() as c:c.execute('INSERT INTO alerts VALUES(?,?,?,?,?,?,?)',(aid,session,query,safe_float(target_price),1,now(),0))
    return {'ok':True,'alert_id':aid}

def discovery_insights():
    init_db()
    with db_conn() as c:
        qs=c.execute("SELECT payload FROM events WHERE event='search' ORDER BY ts DESC LIMIT 500").fetchall()
    cats={}; budgets=[]
    for (raw,) in qs:
        try:
            x=json.loads(raw); cat=x.get('category') or 'general'; cats[cat]=cats.get(cat,0)+1
            if x.get('max_price'):budgets.append(x['max_price'])
        except:pass
    return {'demand_categories':dict(sorted(cats.items(),key=lambda kv:kv[1],reverse=True)[:10]),'avg_declared_budget':round(sum(budgets)/len(budgets),2) if budgets else None,'sample_size':len(qs)}

def score_v1(o,dna=None):
    o.deal=historical_deal(o); base=sum(WEIGHTS[k]*clamp(getattr(o,k)) for k in WEIGHTS); return round(100*clamp(base+(personalize(o,dna or {}) if dna else 0)),2)
def score_value(o,dna=None):
    # Challenger: slightly more conservative on trust/price confidence, never rewards commission heavily.
    base=.24*o.fit+.22*o.trust+.16*o.quality+.16*historical_deal(o)+.10*o.price_confidence+.07*o.satisfaction+.04*o.freshness+.01*o.commission
    return round(100*clamp(base+(personalize(o,dna or {}) if dna else 0)),2)
def champion_score(o,dna=None):
    # Deterministic champion/challenger assignment can be upgraded from observed conversion metrics.
    return max(score_v1(o,dna),score_value(o,dna))
def dedupe(items,dna):
    best={}
    for o in items:
        k=canonical_key(o)
        if k not in best or champion_score(o,dna)>champion_score(best[k],dna):best[k]=o
    return list(best.values())
def persist(items,dna):
    init_db()
    with db_conn() as c:
        for o in items:
            k=canonical_key(o); sc=champion_score(o,dna)
            c.execute('INSERT INTO products VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET price=excluded.price,jet_score=excluded.jet_score,payload=excluded.payload,seen=excluded.seen,url=excluded.url',(k,o.source,o.product_id,o.title,o.url,o.price,o.currency,o.merchant,o.category,sc,json.dumps(asdict(o),ensure_ascii=False),now()))
            last=c.execute('SELECT price,ts FROM price_history WHERE product_key=? ORDER BY ts DESC LIMIT 1',(k,)).fetchone()
            if not last or last[0]!=o.price or now()-last[1]>21600:c.execute('INSERT INTO price_history(product_key,ts,price,currency) VALUES(?,?,?,?)',(k,now(),o.price,o.currency))
def why(o,score):
    bits=[]
    if o.fit>=.8:bits.append('התאמה גבוהה לבקשה')
    if o.trust>=.9:bits.append('מקור בעל אמינות גבוהה')
    if historical_deal(o)>=.7:bits.append('מחיר אטרקטיבי ביחס לנתונים שנצברו')
    if o.quality>=.88:bits.append('איכות חזקה')
    if o.price_confidence>=.85:bits.append('ביטחון מחיר גבוה')
    return ' · '.join(bits) or 'התאמה משוקללת לפי JET Score'
def search(text,session='guest'):
    intent=parse_intent(text); update_dna(session,intent=intent); log_event(session,'search',payload=intent); dna=get_dna(session); items=[]; status=[]
    enabled=[c for c in CONNECTORS if c.enabled()]
    status.extend({'source':c.name,'enabled':False} for c in CONNECTORS if not c.enabled())
    def run_connector(con):
        t0=time.perf_counter()
        try: return con,list(con.search(intent)),round((time.perf_counter()-t0)*1000,1),None
        except Exception as e: return con,[],round((time.perf_counter()-t0)*1000,1),str(e)[:180]
    if enabled:
        with ThreadPoolExecutor(max_workers=min(8,len(enabled))) as pool:
            futures=[pool.submit(run_connector,c) for c in enabled]
            for f in as_completed(futures):
                con,got,ms,err=f.result()
                if err:
                    status.append({'source':con.name,'enabled':True,'error':err,'latency_ms':ms}); record_source_health(con.name,False,ms,0,err); log_event(session,'connector_error',payload={'connector':con.name,'error':err})
                else:
                    items.extend(got); status.append({'source':con.name,'enabled':True,'count':len(got),'latency_ms':ms}); record_source_health(con.name,True,ms,len(got))
    # Identity dedupe remains legacy-compatible; ranking is upgraded.
    items=dedupe(items,dna); persist(items,dna); model=arena_model()
    ranked=sorted(items,key=lambda x:arena_score(x,intent,dna,model),reverse=True); out=[]
    for o in ranked[:24]:
        d=asdict(o); d['jet_score']=arena_score(o,intent,dna,model); d['score_components']={k:round(v*100,1) for k,v in score_components(o,intent,dna).items()}; d['total_cost']=total_cost(o)
        d['savings']=round(o.old_price-o.price,2) if o.old_price and o.old_price>o.price else 0; d['why']=why(o,d['jet_score']); d['deal_confidence']=round(100*price_truth(o)); d['decision']=decision(o,intent,dna); out.append(d)
    return {'intent':intent,'results':out,'sources':status,'dna':{'known_categories':dna.get('categories',{}),'queries':dna.get('queries',0)},'model':model,'need_alternatives':alternatives_hint(intent),'engine':ENGINE_NAME,'version':VERSION}

def record_model_event(model,event,rating=None):
    init_db(); model=str(model or arena_model())[:64]
    cols={'impression':'impressions','outbound_click':'clicks','purchase':'purchases','refund':'refunds'}
    with db_conn() as c:
        c.execute("INSERT OR IGNORE INTO model_stats(model,impressions,clicks,purchases,refunds,satisfaction_sum,satisfaction_n) VALUES(?,0,0,0,0,0,0)",(model,))
        if event in cols: c.execute(f"UPDATE model_stats SET {cols[event]}={cols[event]}+1 WHERE model=?",(model,))
        if rating is not None: c.execute("UPDATE model_stats SET satisfaction_sum=satisfaction_sum+?, satisfaction_n=satisfaction_n+1 WHERE model=?",(max(0,min(5,safe_float(rating))),model))

def alert_candidates():
    init_db(); out=[]
    with db_conn() as c: rows=c.execute('SELECT id,session_id,query,target_price FROM alerts WHERE active=1 ORDER BY created LIMIT 100').fetchall()
    for aid,sess,q,target in rows:
        try:
            r=search(q,sess); hit=next((x for x in r['results'] if not target or x['total_cost']<=target),None)
            if hit: out.append({'alert_id':aid,'query':q,'match':hit})
            with db_conn() as c:c.execute('UPDATE alerts SET last_checked=? WHERE id=?',(now(),aid))
        except Exception: pass
    return out

def feedback(session,body):
    init_db(); fid=str(uuid.uuid4()); rating=round(clamp(safe_float(body.get('rating'))/5)*5,2); purchased=1 if body.get('purchased') else 0
    with db_conn() as c:c.execute('INSERT INTO feedback VALUES(?,?,?,?,?,?,?)',(fid,now(),session,str(body.get('product_id',''))[:256],rating,purchased,str(body.get('note',''))[:1000]))
    log_event(session,'feedback',str(body.get('product_id','')),body); model=str(body.get('model') or arena_model()); record_model_event(model,'purchase' if purchased else 'feedback',rating); return {'ok':True}
def analytics():
    init_db()
    with db_conn() as c:
        ev=dict(c.execute("SELECT event,count(*) FROM events GROUP BY event").fetchall())
        products=c.execute('SELECT count(*) FROM products').fetchone()[0]; users=c.execute('SELECT count(*) FROM dna').fetchone()[0]
        searches=ev.get('search',0); clicks=ev.get('outbound_click',0); purchases=ev.get('purchase',0); refunds=ev.get('refund',0)
        fb=c.execute('SELECT count(*),coalesce(avg(rating),0),coalesce(sum(purchased),0) FROM feedback').fetchone()
        recent=c.execute("SELECT event,count(*) FROM events WHERE ts>? GROUP BY event",(now()-86400,)).fetchall()
        topq=c.execute("SELECT payload,count(*) n FROM events WHERE event='search' GROUP BY payload ORDER BY n DESC LIMIT 10").fetchall()
    funnel={'searches':searches,'clicks':clicks,'purchases':purchases,'refunds':refunds,
            'search_to_click_pct':round(100*clicks/searches,2) if searches else 0,
            'click_to_purchase_pct':round(100*purchases/clicks,2) if clicks else 0}
    satisfaction={'responses':fb[0],'average_rating':round(fb[1],2),'reported_purchases':fb[2]}
    return {'brand':BRAND,'experience_version':EXPERIENCE_VERSION,'events':ev,'last_24h':dict(recent),'funnel':funnel,
            'satisfaction':satisfaction,'products':products,'anonymous_profiles':users,'top_search_payloads':[{'payload':x,'count':n} for x,n in topq],
            'model_arena':{'active':arena_model(),'stats':model_stats()},'source_health':source_health(),'discovery':discovery_insights(),'engine':ENGINE_NAME,'version':VERSION}

def admin_authorized(environ):
    token=env('JET_ADMIN_TOKEN')
    if not token:return False
    supplied=(environ.get('HTTP_AUTHORIZATION') or '').removeprefix('Bearer ').strip()
    return bool(supplied) and hmac.compare_digest(supplied,token)

def json_response(start,status,obj,extra=None):
    b=json.dumps(obj,ensure_ascii=False).encode(); start(f'{status} '+('OK' if status<400 else 'Error'),[('Content-Type','application/json; charset=utf-8'),('Cache-Control','no-store'),*security_headers(),*(extra or [])]);return [b]
def body_json(environ):
    n=min(int(environ.get('CONTENT_LENGTH') or 0),100000); return json.loads(environ['wsgi.input'].read(n) or b'{}')
def app(environ,start):
    path=environ.get('PATH_INFO','/'); method=environ.get('REQUEST_METHOD','GET')
    if path.startswith('/api/') and not rate_allowed(client_ip(environ)):
        return json_response(start,429,{'error':'rate_limited'},[('Retry-After','60')])
    try:
        if path=='/api/search' and method=='POST':
            b=body_json(environ); q=str(b.get('q',''))[:1000]; session=valid_session(b.get('session','guest'))
            if not q.strip():return json_response(start,400,{'error':'query_required'})
            return json_response(start,200,search(q,session))
        if path=='/api/event' and method=='POST':
            b=body_json(environ); s=valid_session(b.get('session','guest')); p=str(b.get('product_id',''))[:256]; e=str(b.get('event','event'))[:64]
            allowed={'impression','outbound_click','purchase','refund','view','save','share'}
            if e not in allowed:return json_response(start,400,{'error':'invalid_event'})
            log_event(s,e,p,b.get('payload'))
            if consent_mode(environ) in ('personalization','analytics','full'): update_dna(s,event=e,product=p)
            if e in ('impression','outbound_click','purchase','refund'): record_model_event((b.get('payload') or {}).get('model') if isinstance(b.get('payload'),dict) else None,e)
            return json_response(start,200,{'ok':True})
        if path=='/api/feedback' and method=='POST':
            b=body_json(environ);return json_response(start,200,feedback(valid_session(b.get('session','guest')),b))
        if path=='/api/analytics':
            if not admin_authorized(environ): return json_response(start,401,{'error':'admin_auth_required'})
            return json_response(start,200,analytics())
        if path=='/api/config':return json_response(start,200,{'version':VERSION,'engine':ENGINE_NAME,'sources':[{'name':c.name,'enabled':c.enabled()} for c in CONNECTORS]})
        if path=='/api/source-health':
            if not admin_authorized(environ): return json_response(start,401,{'error':'admin_auth_required'})
            return json_response(start,200,{'sources':source_health()})
        if path=='/api/discovery':
            if not admin_authorized(environ): return json_response(start,401,{'error':'admin_auth_required'})
            return json_response(start,200,discovery_insights())
        if path=='/api/alert' and method=='POST':
            b=body_json(environ); return json_response(start,200,create_alert(valid_session(b.get('session','guest')),str(b.get('query',''))[:1000],b.get('target_price',0)))
        if path=='/health':return json_response(start,200,{'ok':True,'version':VERSION})
        if path=='/ready':
            ok,err=readiness(); return json_response(start,200 if ok else 503,{'ok':ok,'version':VERSION,'error':err})
        if path=='/api/alerts/check' and method=='POST':
            if not admin_authorized(environ): return json_response(start,401,{'error':'admin_auth_required'})
            return json_response(start,200,{'matches':alert_candidates()})
        if path in ['/admin','/admin.html']:
            data=(ROOT/'admin.html').read_bytes();start('200 OK',[('Content-Type','text/html; charset=utf-8'),('Cache-Control','no-store'),*security_headers()]);return [data]
        if path in ['/','/index.html']:
            data=(ROOT/'index.html').read_bytes();start('200 OK',[('Content-Type','text/html; charset=utf-8'),('Cache-Control','no-cache'),*security_headers()]);return [data]
        start('404 Not Found',[('Content-Type','text/plain; charset=utf-8')]);return [b'Not found']
    except Exception as e:return json_response(start,500,{'error':'internal_error','detail':str(e) if env('JET_DEBUG')=='1' else ''})
def serve():
    from wsgiref.simple_server import make_server
    init_db(); port=int(env('PORT','8000')); print(f'Jet Tesfa JET core v{VERSION} -> http://127.0.0.1:{port}');make_server('0.0.0.0',port,app).serve_forever()
if __name__=='__main__':serve()
