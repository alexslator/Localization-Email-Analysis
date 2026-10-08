import base64
import io
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import app
import inbox
import email_inference as inference
import mail_clients
from test_app import fake_jev, request

class InboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.patch=patch.object(inbox,'DB',Path(self.tmp.name)/'inbox.sqlite3');self.patch.start()
        inbox.STATUS.update(running=False,error=None,stage='Ready')
    def tearDown(self):self.patch.stop();self.tmp.cleanup()
    def test_received_timezone(self):
        with patch.dict(app.os.environ,{'MAIL_TIMEZONE':'Europe/Madrid'}):
            self.assertEqual(inference.local_received('2026-10-06T23:30:00Z').date().isoformat(),'2026-10-07')
    def test_relative_deadlines(self):
        candidates=inference.date_candidates('Please deliver tomorrow; our meeting is on 2026-10-20.','2026-10-06T08:00:00Z')
        self.assertIn('2026-10-07',[x['date'] for x in candidates])
        self.assertIn('2026-10-20',[x['date'] for x in candidates])
    def test_ambiguous_date(self):
        c=inference.date_candidates('Deadline 11/12/2026','2026-10-06T08:00:00Z')[0]
        self.assertTrue(c['ambiguous'])
    def test_no_deadline_invented(self):
        self.assertEqual(inference.date_candidates('Please translate this as soon as possible.','2026-10-06T08:00:00Z'),[])
    def test_inference_choice(self):
        m={'subject':'Translate to French','body':'Into French (France), by tomorrow please.','received':'2026-10-06T08:00:00Z'}
        def fake(p):
            r=fake_jev(p)
            for k,q in p['questions'].items():
                if k.startswith('lang'):
                    chosen='r0' if k=='lang1' else 'not_requested'
                    r['answers'][k].update(choice=chosen,probabilities={x:float(x==chosen) for x in q['criteria']})
            r['answers']['deadline'].update(choice='d0',probabilities={x:float(x=='d0') for x in p['questions']['deadline']['criteria']})
            r['answers']['other_target']['noul']=0
            return r
        with patch.object(app,'call_jev',side_effect=fake):r=inference.infer(m)
        self.assertEqual(r['deadline'],'2026-10-07');self.assertEqual(r['locale'],'French (France)')
    def test_pdf_partial_blocks_capacity(self):
        from pypdf import PdfWriter
        from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject
        stream=io.BytesIO();p=PdfWriter();page=p.add_blank_page(width=600,height=800)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
        content=DecodedStreamObject();content.set_data(b'BT /F1 12 Tf 20 700 Td (These words are readable) Tj ET');page[NameObject('/Contents')]=content
        p.add_blank_page(width=600,height=800);p.write(stream)
        f={'name':'mixed.pdf','data':base64.b64encode(stream.getvalue()).decode()}
        d=app.extract_file(f)
        self.assertEqual(d['count_status'],'partial');self.assertEqual(d['missing_pages'],[2]);self.assertEqual(d['words'],4)
        with patch.object(app,'call_jev',side_effect=fake_jev):r=app.analyze(request(files=[f]))
        self.assertIsNone(r['capacity']['fits'])
        with patch.object(app,'call_jev',side_effect=fake_jev):r=app.analyze(request(files=[f],word_override='100'))
        self.assertTrue(r['capacity']['fits'])
    def test_inbox_count_override_unblocks(self):
        r=inbox.demo()['emails'][0];r['source_documents']=[dict(r['documents'][0],count_status='partial')];r['plan']['word_override']='';inbox.calculate_row(r)
        self.assertIsNone(r['capacity']['fits'])
        r['plan']['word_override']='100';inbox.calculate_row(r);self.assertIsNotNone(r['capacity']['fits'])
    def test_no_attachment_does_not_use_email_word_count(self):
        r=inbox.demo()['emails'][0];r['source_documents']=[];r['documents']=[];inbox.calculate_row(r)
        self.assertIsNone(r['capacity']['fits']);self.assertEqual(r['capacity']['source_words'],0)
    def test_demo_does_not_persist(self):
        inbox.demo();s=inbox.snapshot();self.assertEqual(s['emails'],[]);self.assertEqual(s['runs'],[])
    def test_html_strip(self):
        self.assertEqual(mail_clients.plain('<p>Hello</p><script>bad()</script><div>World</div>').strip(),'Hello\nWorld')
    def fake_client(self,fail_fetch=False):
        received=datetime.fromtimestamp(time.time()-60,timezone.utc).isoformat()
        class Client:
            account='test@example.test'
            def list_ids(self):return ['1','1']
            def get(self,id):
                if fail_fetch:raise RuntimeError('fetch failed')
                return {'id':id,'subject':'Translate','sender':'sender@example.test','received':received,'body':'Translate please'}
            def attachments(self,m):return [],[]
        return Client()
    def run_sync(self,client,processor):
        inbox.LOCK.acquire()
        with patch.object(mail_clients,'client',return_value=client),patch.object(inbox,'process',side_effect=processor):inbox.run('gmail')
    def test_unread_cache_dedup(self):
        client=self.fake_client()
        def process(r,c):r.update(status='not_request',readiness='Not a request');return r
        self.run_sync(client,process)
        first=inbox.snapshot();self.assertEqual(len(first['emails']),1);self.assertEqual(first['runs'][0]['scanned'],1)
        self.assertIsNone(inbox.read_state('watermark:gmail:test@example.test'))
        self.run_sync(client,process);second=inbox.snapshot();self.assertEqual(second['runs'][0]['scanned'],1)
    def test_failed_fetch_keeps_watermark(self):
        self.run_sync(self.fake_client(True),lambda r,c:r)
        self.assertIsNone(inbox.read_state('watermark:gmail:test@example.test'))
        self.assertEqual(inbox.snapshot()['runs'][0]['status'],'partial')
    def test_failed_analysis_retried_beyond_watermark(self):
        client=self.fake_client()
        def fail(r,c):raise RuntimeError('JEV unavailable')
        self.run_sync(client,fail)
        self.assertIsNone(inbox.read_state('watermark:gmail:test@example.test'))
        self.assertEqual(inbox.snapshot()['emails'][0]['status'],'error')
        def success(r,c):r.update(status='not_request',error=None);return r
        self.run_sync(client,success);s=inbox.snapshot()
        self.assertEqual(s['runs'][0]['retried'],1);self.assertEqual(s['runs'][0]['scanned'],1);self.assertEqual(s['emails'][0]['status'],'not_request')
    def test_pagination_gmail(self):
        client=object.__new__(mail_clients.Gmail);client.base='https://gmail.googleapis.com/gmail/v1/users/me';client.session=object()
        with patch.object(mail_clients,'http',side_effect=[{'messages':[{'id':'1'}],'nextPageToken':'next'},{'messages':[{'id':'2'}]}]) as http:
            self.assertEqual(client.list_ids(),['1','2']);self.assertEqual(http.call_count,2)
            self.assertEqual(http.call_args_list[0].kwargs['params']['labelIds'],['INBOX','UNREAD'])
            self.assertNotIn('q',http.call_args_list[0].kwargs['params'])
    def test_pagination_outlook(self):
        client=object.__new__(mail_clients.Outlook);client.base='https://graph.microsoft.com/v1.0';client.session=object()
        with patch.object(mail_clients,'http',side_effect=[{'value':[{'id':'1'}],'@odata.nextLink':client.base+'/next'},{'value':[{'id':'2'}]}]) as http:
            self.assertEqual(client.list_ids(),['1','2']);self.assertEqual(http.call_count,2)
            self.assertEqual(http.call_args_list[0].kwargs['params']['$filter'],'isRead eq false')
    def test_old_unread_and_read_removed_and_returned(self):
        client=self.fake_client()
        original=client.get
        client.get=lambda id:dict(original(id),received='2020-01-01T00:00:00Z')
        inbox.save_state('watermark:gmail:test@example.test',time.time())
        calls=[]
        def process(r,c):calls.append(r['id']);r.update(status='not_request');return r
        self.run_sync(client,process)
        self.assertEqual(len(inbox.snapshot()['runs'][0]['email_ids']),1)
        client.list_ids=lambda:[]
        self.run_sync(client,process)
        self.assertEqual(inbox.snapshot()['runs'][0]['email_ids'],[])
        client.list_ids=lambda:['1']
        self.run_sync(client,process)
        self.assertEqual(inbox.snapshot()['runs'][0]['cached'],1)
        self.assertEqual(len(calls),1)
    def test_read_failed_message_not_retried(self):
        client=self.fake_client()
        def fail(r,c):raise RuntimeError('failed')
        self.run_sync(client,fail)
        client.list_ids=lambda:[]
        self.run_sync(client,fail)
        self.assertEqual(inbox.snapshot()['runs'][0]['retried'],0)
    def test_env_domains_override_legacy_setting(self):
        inbox.save_state('sender_domains',['old.test'])
        with patch.dict(app.os.environ,{'SENDER_DOMAINS':'new.test'}):
            self.assertEqual(inbox.configured_domains(),['new.test'])
    def test_public_row_hides_extracted_text(self):
        r={'body':'email','source_documents':[{'text':'private source'}],'provider_message':{'token':'example'}}
        self.assertEqual(inbox.public(r),{'body':'email'})

if __name__=='__main__':unittest.main()
