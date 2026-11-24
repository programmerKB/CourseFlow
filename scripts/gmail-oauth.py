#!/usr/bin/env python3
"""Obtain a Gmail API refresh token with only the gmail.send scope for the CourseFlow worker.

Before running:
  1. In Google Cloud Console enable the Gmail API for a project.
  2. Configure the OAuth consent screen and add the sender Gmail account as a test user.
  3. Create an OAuth client ID of type "Desktop app".

Run (the client secret is read from GMAIL_CLIENT_SECRET or prompted, never passed on the command line):
  python3 scripts/gmail-oauth.py --client-id 1234-abc.apps.googleusercontent.com [--port 8765] [--manual]

Open the printed URL, sign in as the sender account and allow "Send email on your behalf".
On a remote VM use `ssh -L 8765:127.0.0.1:8765 <vm>` with --port 8765, or --manual to paste the
final browser address. Copy the printed values into .env; keep .env out of Git.
"""
import argparse
import base64
import getpass
import hashlib
import http.server
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
SCOPE = 'https://www.googleapis.com/auth/gmail.send'


def wait_for_code(port, state):
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server API)
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if query.get('state', [''])[0] != state:
                self.send_error(400, 'State mismatch')
                return
            result['code'] = query.get('code', [''])[0]
            result['error'] = query.get('error', [''])[0]
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write('CourseFlow received the authorization. You can close this tab.'.encode())

        def log_message(self, *_args):
            pass

    with http.server.HTTPServer(('127.0.0.1', port), Handler) as server:
        while 'code' not in result:
            server.handle_request()
    if result['error'] or not result['code']:
        sys.exit(f'Authorization was not granted: {result["error"] or "no code"}')
    return result['code']


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--client-id', required=True)
    parser.add_argument('--port', type=int, default=0, help='Loopback port (default: random free port).')
    parser.add_argument('--manual', action='store_true', help='Paste the final browser URL instead of listening.')
    args = parser.parse_args()
    secret = os.environ.get('GMAIL_CLIENT_SECRET') or getpass.getpass('OAuth client secret: ')
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    state = secrets.token_urlsafe(24)
    port = args.port
    if not port:
        with http.server.HTTPServer(('127.0.0.1', 0), http.server.BaseHTTPRequestHandler) as probe:
            port = probe.server_address[1]
    redirect_uri = f'http://127.0.0.1:{port}/'
    url = AUTH_URL + '?' + urllib.parse.urlencode({
        'client_id': args.client_id, 'redirect_uri': redirect_uri, 'response_type': 'code', 'scope': SCOPE,
        'access_type': 'offline', 'prompt': 'consent', 'state': state,
        'code_challenge': challenge, 'code_challenge_method': 'S256'})
    print('Open this URL in a browser and sign in with the sender Gmail account:\n\n' + url + '\n')
    if args.manual:
        final = input('After allowing access, paste the full address shown in the browser: ').strip()
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(final).query)
        if query.get('state', [''])[0] != state or not query.get('code'):
            sys.exit('The pasted address does not contain this request\'s authorization code.')
        code = query['code'][0]
    else:
        print(f'Waiting for the browser redirect on {redirect_uri} ...')
        code = wait_for_code(port, state)
    body = urllib.parse.urlencode({
        'client_id': args.client_id, 'client_secret': secret, 'code': code, 'code_verifier': verifier,
        'grant_type': 'authorization_code', 'redirect_uri': redirect_uri}).encode()
    request = urllib.request.Request(TOKEN_URL, data=body, method='POST',
                                     headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            tokens = json.load(response)
    except urllib.error.HTTPError as error:
        sys.exit(f'Token exchange failed with HTTP {error.code}. Check the client ID, secret and test user.')
    if 'refresh_token' not in tokens:
        sys.exit('Google returned no refresh token. Remove the app at myaccount.google.com/permissions and retry.')
    if SCOPE not in tokens.get('scope', '').split():
        sys.exit('The granted scope does not include gmail.send.')
    print('\nAdd these lines to .env (do not commit them):\n')
    print('GMAIL_TRANSPORT=api')
    print(f'GMAIL_CLIENT_ID={args.client_id}')
    print('GMAIL_CLIENT_SECRET=<the client secret you entered>')
    print(f'GMAIL_REFRESH_TOKEN={tokens["refresh_token"]}')
    print('GMAIL_SENDER=CourseFlow <the-sender-account@gmail.com>')


if __name__ == '__main__':
    main()
