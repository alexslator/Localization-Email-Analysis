"""Durable local inbox queue, unread Inbox scans, and readiness results."""
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
import re
from email.utils import getaddresses
from datetime import datetime, timezone, timedelta
import app
import email_inference
import mail_clients

DB = mail_clients.LOCAL/'inbox.sqlite3'
LOCK=threading.Lock()
STATUS={'running':False,'stage':'Ready','done':0,'total':0,'error':None}

def parse_domains(value):
    if isinstance(value,list):value=','.join(value)
    parts=re.split(r'[,;\s]+',str(value or '').strip())
    domains=[]
    for part in parts:
        if not part:continue
        domain=part.lstrip('@').lower().rstrip('.')
        try:domain=domain.encode('idna').decode('ascii')
        except UnicodeError:raise ValueError('Enter valid sender domains, for example gmail.com.')
        if len(domain)>253 or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',domain):
            raise ValueError('Enter domains only (e.g. gmail.com), separated by commas. No email addresses, URLs or wildcards.')
        if domain not in domains:domains.append(domain)
    return sorted(domains)

def sender_allowed(sender,domains):
    if domains is None:return True  # Internal legacy/test use only; UI runs require a filter.
    addresses=getaddresses([str(sender)])
    if len(addresses)!=1:return False
    address=addresses[0][1]
    if address.count('@')!=1:return False
    try:domain=address.rsplit('@',1)[1].rstrip('.').encode('idna').decode('ascii').lower()
    except UnicodeError:return False
    return domain in domains

def configured_domains():
    return parse_domains(os.getenv('SENDER_DOMAINS',''))


def db():
    DB.parent.mkdir(mode=0o700,exist_ok=True)
    c=sqlite3.connect(DB,timeout=30);c.row_factory=sqlite3.Row
    c.executescript('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY,value TEXT); CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, data TEXT); CREATE TABLE IF NOT EXISTS emails (id TEXT PRIMARY KEY, mailbox TEXT, run_id TEXT, status TEXT, data TEXT);')
    os.chmod(DB,0o600)
    return c

def read_state(key):
    with db() as c:r=c.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
    return json.loads(r[0]) if r else None

def save_state(key,value):
    with db() as c:c.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(key,json.dumps(value)))

def save_run(run):
    with db() as c:c.execute('INSERT OR REPLACE INTO runs VALUES (?,?)',(run['id'],json.dumps(run)))

def save_row(row):
    with db() as c:c.execute('INSERT OR REPLACE INTO emails VALUES (?,?,?,?,?)',(row['id'],row['mailbox'],row['run_id'],row['status'],json.dumps(row)))

def get_row(id):
    with db() as c:r=c.execute('SELECT data FROM emails WHERE id=?',(id,)).fetchone()
    if not r:raise ValueError('Email not found.')
    return json.loads(r[0])

def public(row):
    r={k:v for k,v in row.items() if k not in ('source_documents','provider_message')}
    return r

def snapshot():
    with db() as c:
        runs=[json.loads(r[0]) for r in c.execute('SELECT data FROM runs ORDER BY rowid DESC LIMIT 50')]
        rows=[public(json.loads(r[0])) for r in c.execute('SELECT data FROM emails ORDER BY rowid DESC')]
    return {'status':dict(STATUS),'runs':runs,'emails':rows,'timezone':os.getenv('MAIL_TIMEZONE','Europe/Madrid'),'sender_domains':configured_domains(),'configured':{'gmail':(mail_clients.LOCAL/'gmail-token.json').exists(),'outlook':(mail_clients.LOCAL/'outlook-token.json').exists()}}

def brief_result(documents,brief,locale):
    # File extraction errors do not prevent brief completeness checks.
    payload=app.build_payload(documents,brief,locale)
    raw=app.call_jev(payload);answers=app.normalize(raw,payload)
    docs=[]
    for i,d in enumerate(documents):
        item={k:v for k,v in d.items() if k!='text'}
        item['specialism']=answers[f'd{i}_specialism'];item['document_type']=answers[f'd{i}_document_type'];docs.append(item)
    checks=[]
    for key,(label,_,followup) in app.BRIEF_CHECKS.items():
        p=answers[f'brief_{key}']['noul'];checks.append({'label':label,'probability':p,'status':'stated' if p>=.8 else 'not_found' if p<=.2 else 'confirm','followup':followup})
    return docs,checks,raw.get('model',app.MODEL)

def calculate_row(row):
    plan=row['plan'];issues=[];docs=row.get('source_documents',[])
    extracted=sum(d['words'] for d in docs)
    override=bool(str(plan.get('word_override','')).strip())
    partial=bool(row.get('attachment_warnings')) or any(d.get('count_status')!='estimated' for d in docs)
    reasons=[]
    if not plan.get('deadline'):reasons.append('A confirmed deadline is needed.')
    if not docs and not override:reasons.append('No readable source attachment. Supply the documents or enter a verified total word count.')
    if partial and not override:reasons.append('Incomplete or unverified extraction: enter a verified total source word count before relying on capacity.')
    if plan.get('deadline'):
        try:
            calc=app.capacity(plan,extracted)
            calc['count_status']='verified' if override else 'partial' if partial else 'estimated'
            if reasons:calc['fits']=None
            calc['provisional']=not plan.get('deadline_confirmed',False)
            calc['blocked_reasons']=reasons
        except ValueError as e:calc={'fits':None,'blocked_reasons':[str(e)],'source_words':extracted};reasons.append(str(e))
    else:calc={'fits':None,'blocked_reasons':reasons,'source_words':int(plan['word_override']) if override else extracted,'count_status':'verified' if override else 'partial' if partial else 'estimated'}
    row['capacity']=calc
    checks=row.get('brief_checks',[])
    row['score']=round(100*sum(x['status']=='stated' for x in checks)/len(checks)) if checks else None
    issues.extend(x['followup'] for x in checks if x['status']!='stated')
    issues.extend(row.get('inference',{}).get('notes',[]))
    if plan.get('deadline_confirmed'):
        issues=[x for x in issues if not x.startswith(('Confirm inferred deadline','The date expression','What is the translation delivery deadline','Clarify the conflicting or unsupported deadline'))]
    if plan.get('locale_confirmed'):
        issues=[x for x in issues if not x.startswith(('Confirm inferred target','Confirm whether','Check for target languages','Which target languages and regional variants'))]
    issues.extend(reasons)
    if row.get('inference',{}).get('request_probability',1)<.8:issues.append('Confirm this email is a translation request.')
    if calc.get('fits') is False:issues.append('The requested turnaround exceeds the configured post-editing capacity.')
    for d in row.get('documents',[]):
        for kind in ('specialism','document_type'):
            a=d.get(kind,{})
            if a.get('confidence',0)<.7 or a.get('choice') in ('mixed','other','insufficient_information'):issues.append(f"Confirm {kind.replace('_',' ')} for {d['name']}.")
    row['clarifications']=list(dict.fromkeys(issues))
    row['readiness']='Needs clarification' if issues else 'Initial checks complete'
    return row

def process(row,client):
    message=row['provider_message'];inf=email_inference.infer(message)
    row['inference']=inf
    if inf['request_probability']<=.2:
        row.update(status='not_request',readiness='Not a translation request',score=None,clarifications=[],documents=[],brief_checks=[])
        return row
    files,warnings=client.attachments(message)
    docs=[]
    for file in files:
        try:docs.append(app.extract_file(file))
        except ValueError as e:warnings.append(str(e))
    row['source_documents']=docs;row['attachment_warnings']=warnings
    brief,_=app.sample(message['subject']+'\n'+message['body'],6000)
    # Completeness is checked against original mail, not model-inferred locale labels.
    row['documents'],row['brief_checks'],row['model']=brief_result(docs,brief,'')
    row['plan']={'start':email_inference.local_received(message['received']).date().isoformat(),'deadline':inf['deadline'] or '', 'locale':inf['locale'],'deadline_confirmed':False,'locale_confirmed':False,'rate':5000,'allocation':100,'reserve':0,'word_override':'','holidays':''}
    row['status']='analyzed';row['error']=None
    return calculate_row(row)

def run(provider,domains=None):
    run_id=str(uuid.uuid4());run_start=time.time();run_info=None
    try:
        client=mail_clients.client(provider)
        mailbox=provider+':'+client.account.lower()
        run_info={'id':run_id,'provider':provider,'account':client.account,'until':run_start,'scope':'unread_inbox','scanned':0,'processed_new':0,'analyzed':0,'skipped':0,'errors':0,'retried':0,'retry_succeeded':0,'cached':0,'status':'running','email_ids':[],'sender_domains':domains or [],'excluded_domain':0}
        save_run(run_info);STATUS.update(stage='Fetching unread inbox messages',done=0,total=0,error=None)
        ids=list(dict.fromkeys(client.list_ids()))
        queue=[];fetch_failed=False;retry_ids=set()
        for mid in ids:
            local_id=hashlib.sha256((mailbox+'\0'+mid).encode()).hexdigest()
            try:existing=get_row(local_id)
            except ValueError:existing=None
            try:
                m=existing['provider_message'] if existing and existing['status'] not in ('error','pending') else client.get(mid)
                if not sender_allowed(m['sender'],domains):
                    run_info['excluded_domain']+=1
                    continue
                run_info['email_ids'].append(local_id)
                run_info['scanned']+=1
                if existing and existing['status'] not in ('error','pending'):
                    run_info['cached']+=1
                    continue
                if existing:
                    row=existing;row['provider_message']=m
                    retry_ids.add(local_id);run_info['retried']+=1
                else:
                    row={'id':local_id,'mailbox':mailbox,'provider':provider,'run_id':run_id,'subject':m['subject'],'sender':m['sender'],'received':m['received'],'body':m['body'],'provider_message':m,'status':'pending','score':None,'clarifications':[]}
                save_row(row);queue.append(row)
            except Exception:
                fetch_failed=True;run_info['errors']+=1
        save_run(run_info)
        STATUS.update(total=len(queue),stage='Analyzing emails')
        for index,row in enumerate(queue):
            row['run_id']=run_id
            try:
                row=process(row,client)
                if row['id'] in retry_ids:run_info['retry_succeeded']+=1
                else:
                    run_info['processed_new']+=1
                    if row['status']=='not_request':run_info['skipped']+=1
                    else:run_info['analyzed']+=1
            except Exception as error:
                row.update(status='error',error=str(error) if isinstance(error,(RuntimeError,ValueError)) else 'Analysis failed. Reconnect the mailbox if needed, then retry.',readiness='Analysis failed',score=None)
                run_info['errors']+=1
            save_row(row);save_run(run_info);STATUS.update(done=index+1)
        run_info['status']='partial' if fetch_failed else 'complete';save_run(run_info)
        STATUS.update(stage='Complete' if not fetch_failed else 'Some messages could not be fetched; run again',error=None)
    except Exception as error:
        message=str(error) if isinstance(error,(RuntimeError,ValueError)) else 'Mailbox connection failed. Run connect.py again and retry.'
        STATUS.update(error=message,stage='Run failed')
        if run_info:run_info.update(status='failed',error=message);save_run(run_info)
    finally:
        STATUS['running']=False;LOCK.release()

def start_run():
    provider=os.getenv('MAIL_PROVIDER','gmail').strip().lower()
    if provider not in ('gmail','outlook'):raise ValueError('Set MAIL_PROVIDER to gmail or outlook in .env and restart.')
    if not app.KEY or app.KEY=='paste_your_typesafe_api_key_here':raise ValueError('Add JEV_API_KEY to .env and restart before analyzing your inbox.')
    domains=configured_domains()
    if not domains:raise ValueError('Set at least one allowed sender domain in SENDER_DOMAINS in .env and restart.')
    if not LOCK.acquire(blocking=False):raise ValueError('A run is already in progress.')
    STATUS.update(running=True,stage='Connecting',done=0,total=0,error=None)
    threading.Thread(target=run,args=(provider,domains),daemon=True).start()
    return {'started':True}

def retry(id):
    row=get_row(id)
    if row['status']!='error':raise ValueError('This email is not awaiting retry.')
    return start_run()

def recalculate(data):
    if STATUS['running']:raise ValueError('Wait for the current run to finish before adjusting a request.')
    row=get_row(str(data.get('id','')))
    if row['status']!='analyzed':raise ValueError('Complete the analysis before editing capacity.')
    old=row['plan'];plan=dict(old)
    for key in ['start','deadline','locale','rate','allocation','reserve','word_override','holidays']:plan[key]=data.get(key,old.get(key,''))
    plan['deadline_confirmed']=bool(data.get('deadline_confirmed',False))
    plan['locale_confirmed']=bool(data.get('locale_confirmed',False))
    # Explicit edits to the locale can satisfy its completeness item; re-check only if changed.
    if plan['locale']!=old.get('locale') or plan['locale_confirmed']!=old.get('locale_confirmed'):
        brief,_=app.sample(row['subject']+'\n'+row['body'],6000)
        payload=app.build_payload([],brief,plan['locale'] if plan['locale_confirmed'] else '')
        q=payload['questions']['brief_locale'];payload['questions']={'brief_locale':q}
        a=app.normalize(app.call_jev(payload),payload)['brief_locale']['noul']
        for check in row['brief_checks']:
            if check['label']=='Target locale':check.update(probability=a,status='stated' if a>=.8 else 'not_found' if a<=.2 else 'confirm')
    if plan.get('deadline'):app.capacity(plan,sum(d['words'] for d in row['source_documents']))
    elif str(plan.get('word_override','')).strip():
        # Validate override even when no deadline is set.
        temp=dict(plan);temp['deadline']=temp['start'];app.capacity(temp,sum(d['words'] for d in row['source_documents']))
    row['plan']=plan;calculate_row(row);save_row(row);return public(row)

def demo():
    # Never persisted; cannot affect the live watermark or counters.
    now=datetime.now(timezone.utc);base=now.astimezone(email_inference.local_received(now.isoformat()).tzinfo).date()
    rows=[]
    examples=[('Clinical study summary — French','research@example.test','Clinical / medical','clinical_medical',12000,['Quality expectations','Reference guidance']),('Quarterly report — German','finance@example.test','Financial','financial',8400,['Reference guidance']),('Product launch copy — Spanish','marketing@example.test','Marketing','marketing',3200,[])]
    for i,(subject,sender,_,specialism,count,missing) in enumerate(examples):
        checks=[{'label':v[0],'status':'not_found' if v[0] in missing else 'stated','probability':.1 if v[0] in missing else .95,'followup':v[2]} for v in app.BRIEF_CHECKS.values()]
        doc={'name':f'source-{i+1}.pdf','words':count,'warnings':[],'count_status':'estimated','sampled':False,'specialism':{'choice':specialism,'confidence':.94},'document_type':{'choice':['clinical_document','annual_report','marketing_content'][i],'confidence':.92}}
        row={'id':'demo'+str(i),'provider':'demo','mailbox':'demo','run_id':'demo','subject':subject,'sender':sender,'received':now.isoformat(),'body':'Illustrative email. These results are fixed examples, not live JEV analysis.','status':'analyzed','documents':[doc],'source_documents':[dict(doc,text='Example')],'brief_checks':checks,'attachment_warnings':[],'inference':{'request_probability':.95,'notes':[]},'plan':{'start':base.isoformat(),'deadline':(base+timedelta(days=1 if i==0 else 4)).isoformat(),'locale':['French (France)','German (Germany)','Spanish (Spain)'][i],'rate':5000,'allocation':100,'reserve':0,'word_override':'','holidays':'','deadline_confirmed':True,'locale_confirmed':True}}
        rows.append(public(calculate_row(row)))
    return {'demo':True,'emails':rows,'runs':[{'id':'demo','provider':'demo','account':'Illustrative sample','since':time.time()-86400,'until':time.time(),'scanned':3,'processed_new':3,'analyzed':3,'skipped':0,'errors':0,'retried':0,'status':'complete','email_ids':[r['id'] for r in rows]}],'status':dict(STATUS,running=False),'configured':{'gmail':False,'outlook':False}}


def recover_interrupted():
    with db() as c:
        records=c.execute('SELECT id,data FROM runs').fetchall()
        for record in records:
            run=json.loads(record['data'])
            if run.get('status')=='running':
                run['status']='interrupted'
                c.execute('UPDATE runs SET data=? WHERE id=?',(json.dumps(run),record['id']))


def reanalyze(id):
    row=get_row(id)
    domains=configured_domains()
    if not domains or not sender_allowed(row['sender'],domains):
        raise ValueError('This sender is outside your allowed domains. Update SENDER_DOMAINS in .env and restart before reanalyzing.')
    if not app.KEY or app.KEY=='paste_your_typesafe_api_key_here':raise ValueError('Configure your JEV key first.')
    if not LOCK.acquire(blocking=False):raise ValueError('A run is already in progress.')
    STATUS.update(running=True,stage='Reanalyzing selected email',done=0,total=1,error=None)
    def work():
        try:
            client=mail_clients.client(row['provider'])
            if row['mailbox']!=row['provider']+':'+client.account.lower():raise ValueError('Reconnect the original mailbox to reanalyze this email.')
            message=client.get(row['provider_message']['id'])
            if not sender_allowed(message['sender'],domains):raise ValueError('The sender does not match the domain filter.')
            old_plan=row.get('plan')
            row['provider_message']=message
            updated=process(row,client)
            if old_plan and updated['status']=='analyzed':
                updated['plan']=old_plan
                calculate_row(updated)
            save_row(updated)
            STATUS.update(stage='Selected email updated',done=1)
        except Exception as error:
            STATUS.update(error=str(error) if isinstance(error,(RuntimeError,ValueError)) else 'Could not reanalyze this email. Try reconnecting the mailbox.',stage='Reanalysis failed')
        finally:
            STATUS['running']=False;LOCK.release()
    threading.Thread(target=work,daemon=True).start()
    return {'started':True}
