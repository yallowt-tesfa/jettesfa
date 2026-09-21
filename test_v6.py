import os,tempfile,unittest,warnings,random,json,io,threading
os.environ['JET_DEMO']='1'
import jet_app as j

class V6(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); j.DB=j.Path(self.tmp.name)/'t.db'; j._RATE.clear(); j.init_db()
 def tearDown(self): self.tmp.cleanup()
 def o(self,**kw):
  x=dict(source='x',product_id=str(random.random()),title='75 TV',url='https://x.test/p',price=3000,currency='ILS',category='tv',merchant='M',quality=.9,trust=.9,fit=.9,deal=.8,freshness=.9,price_confidence=.9,satisfaction=.8,commission=.1,available=True);x.update(kw);return j.Opportunity(**x)
 def req(self,path='/',method='GET',body=None,headers=None):
  raw=json.dumps(body or {}).encode(); env={'PATH_INFO':path,'REQUEST_METHOD':method,'CONTENT_LENGTH':str(len(raw)),'wsgi.input':io.BytesIO(raw),'REMOTE_ADDR':'1.2.3.4'}; env.update(headers or {}); cap={}
  def start(s,h):cap['s']=s;cap['h']=dict(h)
  out=b''.join(j.app(env,start)); return int(cap['s'].split()[0]),cap['h'],json.loads(out) if cap['h'].get('Content-Type','').startswith('application/json') else out
 def test_01_version(self):self.assertEqual(j.VERSION,'9.0.0')
 def test_02_intent_he(self):
  x=j.parse_intent('טלוויזיה 75 עד 4,000 שקל איכותית');self.assertEqual(x['category'],'tv');self.assertEqual(x['max_price'],4000)
 def test_03_intent_en(self):self.assertEqual(j.parse_intent('premium laptop under 5000')['category'],'laptop')
 def test_04_total_cost(self):self.assertEqual(j.total_cost(self.o(shipping=100,metadata={'tax':50})),3150)
 def test_05_unavailable(self):self.assertEqual(j.decision(self.o(available=False),{},{}),'AVOID')
 def test_06_budget(self):self.assertEqual(j.decision(self.o(price=5000),{'max_price':3000},{}),'ALTERNATIVE')
 def test_07_commission_guard(self):self.assertGreater(j.rank_truth(self.o(commission=0,trust=.95,quality=.95,fit=.95,price_confidence=.95),{},{}),j.rank_truth(self.o(commission=1,trust=.2,quality=.2,fit=.3,price_confidence=.2),{},{}))
 def test_08_gtin(self):self.assertEqual(j.canonical_key(self.o(gtin='123-45')),'gtin:12345')
 def test_09_dedupe(self):self.assertEqual(len(j.dedupe([self.o(gtin='1'),self.o(gtin='1')],{})),1)
 def test_10_dna(self):j.update_dna('u',intent={'category':'tv','max_price':4000});self.assertEqual(j.get_dna('u')['categories']['tv'],1)
 def test_11_alert(self):self.assertTrue(j.create_alert('u','tv',4000)['ok'])
 def test_12_discovery(self):j.log_event('u','search',payload={'category':'tv'});self.assertEqual(j.discovery_insights()['demand_categories']['tv'],1)
 def test_13_health(self):self.assertTrue(j.readiness()[0])
 def test_14_source_health(self):j.record_source_health('x',True,1,2);self.assertTrue(j.source_health()[0]['ok'])
 def test_15_arena_default(self):self.assertEqual(j.arena_model(),'truth_v6')
 def test_16_arena_real_threshold(self):
  for m in ['truth_v6','conservative_v6']:
   for _ in range(99):j.record_model_event(m,'impression')
  self.assertEqual(j.arena_model(),'truth_v6')
 def test_17_model_purchase(self):j.record_model_event('truth_v6','purchase');self.assertEqual(j.model_stats()['truth_v6']['purchases'],1)
 def test_18_model_refund(self):j.record_model_event('truth_v6','refund');self.assertEqual(j.model_stats()['truth_v6']['refunds'],1)
 def test_19_search(self):
  x=j.search('טלוויזיה עד 4000','u');self.assertEqual(x['version'],'9.0.0');self.assertTrue(x['results'])
 def test_20_rank_order(self):
  x=j.search('טלוויזיה עד 4000','u')['results'];self.assertGreaterEqual(x[0]['jet_score'],x[-1]['jet_score'])
 def test_21_components(self):self.assertIn('confidence',j.score_components(self.o(),{},{}))
 def test_22_confidence(self):self.assertGreater(j.confidence_score(self.o()),j.confidence_score(self.o(title='',url='',merchant='',price_confidence=.1)))
 def test_23_fake_discount(self):self.assertLessEqual(j.price_truth(self.o(price=3000,old_price=2500)),.8)
 def test_24_session_sanitize(self):self.assertEqual(j.valid_session('a <b>'),'ab')
 def test_25_rate(self):
  self.assertTrue(j.rate_allowed('x',2,60));self.assertTrue(j.rate_allowed('x',2,60));self.assertFalse(j.rate_allowed('x',2,60))
 def test_26_api_health(self):self.assertEqual(self.req('/health')[0],200)
 def test_27_api_ready(self):self.assertEqual(self.req('/ready')[0],200)
 def test_28_api_search(self):self.assertEqual(self.req('/api/search','POST',{'q':'tv 4000','session':'u'})[0],200)
 def test_29_empty_query(self):self.assertEqual(self.req('/api/search','POST',{'q':''})[0],400)
 def test_30_security_headers(self):self.assertEqual(self.req('/health')[1]['X-Content-Type-Options'],'nosniff')
 def test_31_not_found(self):self.assertEqual(self.req('/missing')[0],404)
 def test_32_event(self):self.assertEqual(self.req('/api/event','POST',{'session':'u','event':'outbound_click','product_id':'p','payload':{'model':'truth_v6'}}, {'HTTP_X_JET_CONSENT':'analytics'})[0],200)
 def test_33_feedback(self):self.assertTrue(j.feedback('u',{'product_id':'p','rating':5,'purchased':True,'model':'truth_v6'})['ok'])
 def test_34_alert_check(self):j.create_alert('u','טלוויזיה',5000);self.assertIsInstance(j.alert_candidates(),list)
 def test_35_failed_connector(self):
  class B:
   name='bad'
   def enabled(self):return True
   def search(self,i):raise RuntimeError('boom')
  old=j.CONNECTORS;j.CONNECTORS=[B(),j.DemoConnector()]
  try:self.assertTrue(j.search('tv 4000','u')['results'])
  finally:j.CONNECTORS=old
 def test_36_resourcewarnings(self):
  with warnings.catch_warnings(record=True) as w:
   warnings.simplefilter('always',ResourceWarning);j.search('tv 4000','u');j.analytics();self.assertFalse([x for x in w if issubclass(x.category,ResourceWarning)])
 def test_37_concurrent_reads(self):
  errs=[]
  def f():
   try:j.search('tv 4000',str(random.random()))
   except Exception as e:errs.append(e)
  ts=[threading.Thread(target=f) for _ in range(8)];[t.start() for t in ts];[t.join() for t in ts];self.assertFalse(errs)
 def test_38_long_query_capped(self):
  code,_,_=self.req('/api/search','POST',{'q':'tv '+('x'*5000)});self.assertEqual(code,200)
 def test_39_analytics(self):self.assertIn('model_arena',j.analytics())
 def test_40_alternatives(self):self.assertGreaterEqual(len(j.alternatives_hint({'category':'tv'})),2)

if __name__=='__main__':unittest.main(verbosity=2)
