"""One-time read-only account authorization, run on the user's computer."""
import argparse
import os
import app
from mail_clients import LOCAL, private_write

def main():
    parser=argparse.ArgumentParser();parser.add_argument('provider',choices=['gmail','outlook']);args=parser.parse_args()
    # requests and Google/MSAL use the same verified CA bundle as the local app.
    import certifi
    os.environ.setdefault('REQUESTS_CA_BUNDLE',os.getenv('SSL_CERT_FILE') or certifi.where())
    if args.provider=='gmail':
        from google_auth_oauthlib.flow import InstalledAppFlow
        filename=app.ROOT/os.getenv('GMAIL_CREDENTIALS_FILE','gmail-credentials.json')
        if not filename.exists():raise SystemExit('Download a Desktop app OAuth client JSON from Google Cloud and save it as gmail-credentials.json. See README.md.')
        flow=InstalledAppFlow.from_client_secrets_file(str(filename),['https://www.googleapis.com/auth/gmail.readonly'])
        creds=flow.run_local_server(port=0,access_type='offline',prompt='consent select_account')
        private_write(LOCAL/'gmail-token.json',creds.to_json())
    else:
        import msal
        client_id=os.getenv('OUTLOOK_CLIENT_ID')
        if not client_id:raise SystemExit('Add OUTLOOK_CLIENT_ID to .env. See README.md for the app registration steps.')
        cache=msal.SerializableTokenCache()
        auth=msal.PublicClientApplication(client_id,authority='https://login.microsoftonline.com/'+os.getenv('OUTLOOK_TENANT','common'),token_cache=cache)
        flow=auth.initiate_device_flow(scopes=['Mail.Read','User.Read'])
        if 'user_code' not in flow:raise SystemExit('Could not start sign-in. Confirm the tenant, client ID and public-client setting.')
        print(flow['message'],flush=True)
        result=auth.acquire_token_by_device_flow(flow)
        if 'access_token' not in result:raise SystemExit('Sign-in failed: '+str(result.get('error_description',result.get('error','Unknown error'))))
        private_write(LOCAL/'outlook-token.json',cache.serialize())
    print('Connected. Start python3 app.py, select your provider, and click Run inbox check. No emails have been analyzed yet.')

if __name__=='__main__':main()
