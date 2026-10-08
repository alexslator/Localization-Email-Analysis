import base64,io,time,unittest,tempfile
from pathlib import Path
from unittest.mock import patch
import app,inbox,mail_clients
import test_inbox

class FilterTests(unittest.TestCase):
 def test_domains_normalize(self):
  self.assertEqual(inbox.parse_domains('@Gmail.com, example.com gmail.com'),['example.com','gmail.com'])
 def test_exact_address_match(self):
  self.assertTrue(inbox.sender_allowed('A Person <person@GMAIL.com>',['gmail.com']))
  for value in ['a@notgmail.com','a@gmail.com.attacker.com','a@mail.gmail.com','gmail.com <a@other.com>','a@gmail.com, b@other.com']:
   self.assertFalse(inbox.sender_allowed(value,['gmail.com']))
 def test_invalid_domain_filter(self):
  for value in ['a@gmail.com','https://gmail.com','*.gmail.com','gmail']:
   with self.assertRaises(ValueError):inbox.parse_domains(value)
 def test_large_attachment_passes_mail_adapter(self):
  data=b'x'*(11*1024*1024)
  f,w=mail_clients.attachment('large.pdf',data)
  self.assertIsNone(w);self.assertIsNotNone(f)
  files,warnings=mail_clients.limit_files([f,f],[])
  self.assertEqual(len(files),2);self.assertFalse(warnings)
 def test_large_long_pdf_extracts(self):
  from pypdf import PdfWriter
  from pypdf.generic import NameObject,DictionaryObject,DecodedStreamObject
  writer=PdfWriter()
  for i in range(301):
   page=writer.add_blank_page(width=600,height=800)
   font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
   page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
   content=DecodedStreamObject();content.set_data(b'BT /F1 12 Tf 20 700 Td (Readable page) Tj ET');page[NameObject('/Contents')]=content
  writer.add_metadata({'/TestPadding':'x'*(11*1024*1024)})
  out=io.BytesIO();writer.write(out)
  result=app.extract_file({'name':'large.pdf','data':base64.b64encode(out.getvalue()).decode()})
  self.assertEqual(result['words'],602);self.assertEqual(result['count_status'],'estimated')
 def test_domain_filter_before_model(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(inbox,'DB',Path(tmp)/'db'):
   client=test_inbox.InboxTests().fake_client()
   inbox.LOCK.acquire()
   with patch.object(mail_clients,'client',return_value=client),patch.object(inbox,'process') as process:
    inbox.run('gmail',['gmail.com'])
   process.assert_not_called()
   run=inbox.snapshot()['runs'][0]
   self.assertEqual(run['excluded_domain'],1);self.assertEqual(run['processed_new'],0)
   self.assertEqual(inbox.snapshot()['emails'],[])
 def test_no_filter_does_not_start(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(inbox,'DB',Path(tmp)/'db'),patch.object(app,'KEY','test'),patch.dict(app.os.environ,{'SENDER_DOMAINS':''}):
   with self.assertRaisesRegex(ValueError,'at least one'):inbox.start_run()

if __name__=='__main__':unittest.main()
