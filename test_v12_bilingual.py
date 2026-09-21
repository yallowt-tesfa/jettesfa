import pathlib, unittest
ROOT=pathlib.Path(__file__).parent
HTML=(ROOT/'index.html').read_text(encoding='utf-8')
class BilingualUI(unittest.TestCase):
    def test_language_buttons(self):
        self.assertIn('id="langHe"',HTML); self.assertIn('id="langEn"',HTML)
    def test_rtl_ltr_switch(self):
        self.assertIn("document.documentElement.dir=uiLang==='he'?'rtl':'ltr'",HTML)
    def test_persists_language(self): self.assertIn('localStorage.jet_lang',HTML)
    def test_english_content(self):
        for text in ['What do you','Find it for me','Know when to wait']:
            self.assertIn(text,HTML)
    def test_hebrew_content(self):
        for text in ['מה אתם','מצא לי','גם לדעת לחכות']:
            self.assertIn(text,HTML)
    def test_voice_tracks_language(self):
        self.assertIn("uiLang==='he'?'he-IL':'en-US'",HTML)
    def test_no_amharic_toggle(self): self.assertNotIn('langAm',HTML)
class Deployment(unittest.TestCase):
    def test_procfile_v12(self): self.assertIn('jet_v12:app',(ROOT/'Procfile').read_text())
    def test_railway_v12(self): self.assertIn('jet_v12:app',(ROOT/'railway.toml').read_text())
    def test_version(self): self.assertIn('VERSION = "12.0.0"',(ROOT/'jet_v12.py').read_text())
if __name__=='__main__': unittest.main(verbosity=2)
