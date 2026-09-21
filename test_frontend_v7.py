from pathlib import Path
import unittest
H=(Path(__file__).with_name('index.html')).read_text(encoding='utf-8')
class FrontendV7(unittest.TestCase):
 def test_need_first(self): self.assertIn('מה אתם <span>צריכים היום?</span>',H)
 def test_voice_input(self): self.assertIn('SpeechRecognition',H); self.assertIn("recognition.lang='he-IL'",H)
 def test_voice_output(self): self.assertIn('SpeechSynthesisUtterance',H)
 def test_api_search(self): self.assertIn("fetch('/api/search'",H)
 def test_no_customer_dna_marketing(self): self.assertNotIn('Customer DNA',H)
 def test_no_commission_marketing(self): self.assertNotIn('עמלה',H)
 def test_no_internal_score_label(self): self.assertNotIn('JET ${x.jet_score}',H)
 def test_affiliate_disclosure(self): self.assertIn('קישורי שותפים',H)
 def test_fallback_copy(self): self.assertIn('לא תומך כרגע בזיהוי דיבור',H)
if __name__=='__main__':unittest.main()
