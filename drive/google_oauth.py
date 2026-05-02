import json

from django.conf import settings
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow


def _client_config():
    client_id = settings.GOOGLE_OAUTH_CLIENT_ID
    client_secret = settings.GOOGLE_OAUTH_CLIENT_SECRET
    if not client_id or not client_secret:
        raise RuntimeError(
            'Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in the environment.',
        )
    return {
        'web': {
            'client_id': client_id,
            'client_secret': client_secret,
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token',
            'redirect_uris': [settings.GOOGLE_OAUTH_REDIRECT_URI],
        },
    }


def oauth_scopes():
    return list(settings.GOOGLE_OAUTH_SCOPES)


def make_flow():
    flow = Flow.from_client_config(_client_config(), scopes=oauth_scopes())
    flow.redirect_uri = settings.GOOGLE_OAUTH_REDIRECT_URI
    return flow


def authorization_url(request):
    flow = make_flow()
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent',
    )
    request.session['google_oauth_state'] = state
    request.session.modified = True
    return authorization_url


def exchange_code(request, code, state):
    expected = request.session.get('google_oauth_state')
    if not expected or expected != state:
        raise ValueError('Invalid OAuth state')
    flow = make_flow()
    flow.fetch_token(code=code)
    request.session.pop('google_oauth_state', None)
    creds = flow.credentials
    request.session['google_credentials'] = creds.to_json()
    request.session.modified = True
    return creds


def credentials_from_session(request) -> Credentials | None:
    raw = request.session.get('google_credentials')
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return Credentials.from_authorized_user_info(data)
    except (ValueError, KeyError):
        return None


def get_credentials(request) -> Credentials | None:
    creds = credentials_from_session(request)
    if not creds:
        return None
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        request.session['google_credentials'] = creds.to_json()
        request.session.modified = True
    elif creds.expired:
        request.session.pop('google_credentials', None)
        return None
    return creds


def clear_session(request):
    request.session.pop('google_credentials', None)
    request.session.pop('google_oauth_state', None)
    request.session.modified = True
