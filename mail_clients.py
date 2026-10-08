"""Read-only Gmail and Microsoft Graph adapters. No send/write scopes or methods."""
import base64
import json
import os
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote
import app

LOCAL = app.ROOT / '.local'
SUPPORTED = {'.pdf','.docx','.txt','.md'}

def private_write(path, text):
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
    with os.fdopen(fd,'w') as f: f.write(text)
    os.replace(temporary,path)

class PlainHTML(HTMLParser):
    def __init__(self): super().__init__(); self.parts=[]; self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'): self.hidden+=1
        if tag in ('br','p','div','li','tr'): self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style'): self.hidden=max(0,self.hidden-1)
    def handle_data(self,data):
        if not self.hidden:self.parts.append(data)

def plain(text):
    p=PlainHTML();p.feed(text);return ''.join(p.parts)

def b64decode(value): return base64.urlsafe_b64decode(value+'='*(-len(value)%4))

def http(session,url,**kwargs):
    for attempt in range(3):
        r=session.get(url,timeout=60,**kwargs)
        if r.status_code in (429,503,504) and attempt<2:
            try:delay=min(15,max(1,int(r.headers.get('Retry-After','2'))))
            except ValueError:delay=2
            time.sleep(delay);continue
        if not r.ok:raise RuntimeError(f'Mail provider returned HTTP {r.status_code}. Run connect.py again if authorization expired.')
        return r.json()

def attachment(name,data,size=None):
    if Path(name).suffix.lower() not in SUPPORTED:return None,f'{name}: unsupported attachment; not counted.'
    return {'name':name,'data':base64.b64encode(data).decode()},None

class Gmail:
    def __init__(self):
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import AuthorizedSession, Request
        except ImportError:raise RuntimeError('Install requirements.txt to enable Gmail.')
        path=LOCAL/'gmail-token.json'
        if not path.exists():raise RuntimeError('Connect Gmail first: python3 connect.py gmail')
        creds=Credentials.from_authorized_user_file(str(path),['https://www.googleapis.com/auth/gmail.readonly'])
        if not creds.valid:
            if not creds.refresh_token:raise RuntimeError('Run python3 connect.py gmail to reconnect.')
            creds.refresh(Request());private_write(path,creds.to_json())
        self.session=AuthorizedSession(creds)
        self.base='https://gmail.googleapis.com/gmail/v1/users/me'
        self.account=http(self.session,self.base+'/profile')['emailAddress']
    def list_ids(self):
        params={'labelIds':['INBOX','UNREAD'],'maxResults':100}
        ids=[]
        while True:
            data=http(self.session,self.base+'/messages',params=params)
            ids.extend(m['id'] for m in data.get('messages',[]))
            token=data.get('nextPageToken')
            if not token:return ids
            params['pageToken']=token
    def get(self,message_id):
        data=http(self.session,self.base+'/messages/'+quote(message_id,safe=''),params={'format':'full'})
        headers={h['name'].lower():h['value'] for h in data.get('payload',{}).get('headers',[])}
        text=[];html=[];parts=[]
        def walk(p):
            if p.get('filename'):
                parts.append(p);return
            body=p.get('body',{}).get('data')
            if body:
                decoded=b64decode(body).decode('utf-8',errors='replace')
                if p.get('mimeType')=='text/plain':text.append(decoded)
                elif p.get('mimeType')=='text/html':html.append(plain(decoded))
            for child in p.get('parts',[]):walk(child)
        walk(data.get('payload',{}))
        return {'id':message_id,'subject':headers.get('subject','(No subject)'),'sender':headers.get('from',''),'received':datetime.fromtimestamp(int(data['internalDate'])/1000,timezone.utc).isoformat(),'body':'\n'.join(text or html),'attachments_meta':parts}
    def attachments(self,message):
        files=[];warnings=[]
        for p in message['attachments_meta']:
            name=p['filename'];body=p.get('body',{});size=body.get('size',0)
            if Path(name).suffix.lower() not in SUPPORTED:
                # Inline signature images do not count as missing translation documents.
                headers={h['name'].lower():h['value'] for h in p.get('headers',[])}
                if headers.get('content-disposition','').lower().startswith('inline'):continue
                warnings.append(f'{name}: unsupported attachment; not counted.');continue
            encoded=body.get('data')
            if not encoded and body.get('attachmentId'):
                encoded=http(self.session,self.base+'/messages/'+quote(message['id'],safe='')+'/attachments/'+quote(body['attachmentId'],safe=''))['data']
            if not encoded:warnings.append(f'{name}: attachment content unavailable.');continue
            f,w=attachment(name,b64decode(encoded),size)
            if f:files.append(f)
            if w:warnings.append(w)
        return limit_files(files,warnings)

class Outlook:
    def __init__(self):
        try:import msal;import requests
        except ImportError:raise RuntimeError('Install requirements.txt to enable Outlook.')
        client_id=os.getenv('OUTLOOK_CLIENT_ID','')
        if not client_id:raise RuntimeError('Set OUTLOOK_CLIENT_ID in .env and run python3 connect.py outlook.')
        cache=msal.SerializableTokenCache();path=LOCAL/'outlook-token.json'
        if path.exists():cache.deserialize(path.read_text())
        auth=msal.PublicClientApplication(client_id,authority='https://login.microsoftonline.com/'+os.getenv('OUTLOOK_TENANT','common'),token_cache=cache)
        accounts=auth.get_accounts()
        if len(accounts)!=1:raise RuntimeError('Connect one Outlook account using python3 connect.py outlook.')
        result=auth.acquire_token_silent(['Mail.Read','User.Read'],account=accounts[0])
        if not result or 'access_token' not in result:raise RuntimeError('Reconnect Outlook: python3 connect.py outlook')
        if cache.has_state_changed:private_write(path,cache.serialize())
        self.session=requests.Session();self.session.headers.update({'Authorization':'Bearer '+result['access_token'],'Prefer':'outlook.body-content-type="text", IdType="ImmutableId"'})
        self.base='https://graph.microsoft.com/v1.0'
        user=http(self.session,self.base+'/me',params={'$select':'id,mail,userPrincipalName'})
        self.account=user.get('mail') or user.get('userPrincipalName') or user['id']
    def list_ids(self):
        url=self.base+'/me/mailFolders/inbox/messages'
        params={'$filter':'isRead eq false','$select':'id,receivedDateTime','$top':100}
        ids=[]
        while url:
            data=http(self.session,url,params=params);ids.extend(m['id'] for m in data.get('value',[]));url=data.get('@odata.nextLink');params=None
            if url and not url.startswith(self.base+'/'):raise RuntimeError('Unexpected Microsoft paging URL.')
        return ids
    def get(self,message_id):
        d=http(self.session,self.base+'/me/messages/'+quote(message_id,safe=''),params={'$select':'id,subject,from,receivedDateTime,body,hasAttachments'})
        body=d.get('body',{});sender=d.get('from',{}).get('emailAddress',{})
        return {'id':message_id,'subject':d.get('subject','(No subject)'),'sender':sender.get('address',''),'received':d['receivedDateTime'],'body':plain(body.get('content','')) if body.get('contentType','').lower()=='html' else body.get('content',''),'has_attachments':d.get('hasAttachments',False)}
    def attachments(self,message):
        files=[];warnings=[]
        if not message.get('has_attachments'):return files,warnings
        url=self.base+'/me/messages/'+quote(message['id'],safe='')+'/attachments'
        while url:
            data=http(self.session,url)
            for p in data.get('value',[]):
                if p.get('isInline'):continue
                name=p.get('name','attachment')
                if p.get('@odata.type')!='#microsoft.graph.fileAttachment':warnings.append(f'{name}: linked or embedded attachment needs manual retrieval.');continue
                f,w=attachment(name,base64.b64decode(p.get('contentBytes','')),p.get('size',0))
                if f:files.append(f)
                if w:warnings.append(w)
            url=data.get('@odata.nextLink')
            if url and not url.startswith(self.base+'/'):raise RuntimeError('Unexpected Microsoft paging URL.')
        return limit_files(files,warnings)

def limit_files(files,warnings):
    kept=[];total=0
    for f in files:
        size=len(f['data'])*3//4
        if len(kept)>=10:warnings.append(f"{f['name']}: exceeds the 10-document count limit; not counted.")
        else:kept.append(f);total+=size
    return kept,warnings

def client(provider):
    if provider=='gmail':return Gmail()
    if provider=='outlook':return Outlook()
    raise ValueError('Choose Gmail or Outlook.')
