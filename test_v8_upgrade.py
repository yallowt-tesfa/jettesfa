import os, io, json, unittest
os.environ['JET_DEMO']='1'; os.environ['JET_ADMIN_TOKEN']='unit-test-secret'
import jet_app as j
class V8(unittest.TestCase):
 def req(self,path,headers=None):
  env={'PATH_INFO':path,'REQUEST_METHOD':'GET','CONTENT_LENGTH':'0','wsgi.input':io.BytesIO(b''),'REMOTE_ADDR':'9.9.9.9'};env.update(headers or {});cap={}
  def start(s,h):cap['s']=s;cap['h']=dict(h)
  body=b''.join(j.app(env,start));return int(cap['s'].split()[0]),body
 def test_brand(self):self.assertEqual(j.BRAND,'Jet Tesfa')
 def test_admin_denied(self):self.assertEqual(self.req('/api/analytics')[0],401)
 def test_admin_allowed(self):self.assertEqual(self.req('/api/analytics',{'HTTP_AUTHORIZATION':'Bearer unit-test-secret'})[0],200)
 def test_admin_page(self):self.assertEqual(self.req('/admin')[0],200)
 def test_funnel(self):self.assertIn('funnel',j.analytics())
if __name__=='__main__':unittest.main(verbosity=2)
