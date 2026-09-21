import io, os, unittest
os.environ['JET_DEMO']='1'; os.environ['JET_ADMIN_TOKEN']='unit-test-secret'
import jet_app as j
class V9Hardening(unittest.TestCase):
 def req(self,path,method='GET',body=b'',headers=None):
  env={'PATH_INFO':path,'REQUEST_METHOD':method,'CONTENT_LENGTH':str(len(body)),'wsgi.input':io.BytesIO(body),'REMOTE_ADDR':'8.8.8.8'}; env.update(headers or {}); cap={}
  def start(s,h): cap['s']=s; cap['h']=dict(h)
  data=b''.join(j.app(env,start)); return int(cap['s'].split()[0]),cap['h'],data
 def test_version(self): self.assertEqual(j.VERSION,'9.0.0')
 def test_alert_check_requires_admin(self): self.assertEqual(self.req('/api/alerts/check','POST')[0],401)
 def test_alert_check_admin(self): self.assertEqual(self.req('/api/alerts/check','POST',headers={'HTTP_AUTHORIZATION':'Bearer unit-test-secret'})[0],200)
 def test_invalid_event_rejected(self): self.assertEqual(self.req('/api/event','POST',b'{"event":"made_up"}')[0],400)
 def test_security_headers(self):
  s,h,b=self.req('/health'); self.assertEqual(s,200); self.assertIn('Content-Security-Policy',h); self.assertEqual(h.get('X-Content-Type-Options'),'nosniff')
 def test_prod_rejects_weak_config(self):
  old={k:os.environ.get(k) for k in ('JET_ENV','JET_ADMIN_TOKEN','JET_DEMO','JET_DEBUG')}
  try:
   os.environ['JET_ENV']='production'; os.environ['JET_ADMIN_TOKEN']='short'; os.environ['JET_DEMO']='0'; os.environ['JET_DEBUG']='0'
   ok,err=j.readiness(); self.assertFalse(ok); self.assertEqual(err,'secure_admin_token_required')
  finally:
   for k,v in old.items():
    if v is None: os.environ.pop(k,None)
    else: os.environ[k]=v
 def test_commission_weight_guard(self): self.assertLessEqual(j.WEIGHTS['commission'],.03)
 def test_parallel_search_keeps_results(self): self.assertTrue(j.search('laptop under 5000','v9')['results'])
if __name__=='__main__': unittest.main(verbosity=2)
