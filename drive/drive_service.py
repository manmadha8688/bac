from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .google_oauth import get_credentials


def drive_client(request):
    creds = get_credentials(request)
    if not creds:
        return None
    return build('drive', 'v3', credentials=creds, cache_discovery=False)


def list_folders(request, page_size=50):
    service = drive_client(request)
    if not service:
        return None, 'not_authenticated'
    try:
        resp = (
            service.files()
            .list(
                q=(
                    "mimeType='application/vnd.google-apps.folder' "
                    "and 'root' in parents and trashed=false"
                ),
                pageSize=page_size,
                fields='nextPageToken, files(id, name, modifiedTime)',
                orderBy='folder,name_natural',
            )
            .execute()
        )
        files = resp.get('files', [])
        folders = [
            {'id': f['id'], 'name': f['name'], 'modifiedTime': f.get('modifiedTime')}
            for f in files
        ]
        return folders, None
    except HttpError as e:
        return None, str(e)


def list_files_in_folder(request, folder_id, page_size=100):
    service = drive_client(request)
    if not service:
        return None, 'not_authenticated'
    if not folder_id:
        return [], None
    safe_id = folder_id.replace("'", "\\'")
    q = f"'{safe_id}' in parents and trashed=false"
    try:
        resp = (
            service.files()
            .list(
                q=q,
                pageSize=page_size,
                fields='nextPageToken, files(id, name, mimeType, modifiedTime, size)',
                orderBy='folder,name_natural',
            )
            .execute()
        )
        files = resp.get('files', [])
        documents = [
            {
                'id': f['id'],
                'name': f['name'],
                'mimeType': f.get('mimeType', ''),
                'modifiedTime': f.get('modifiedTime'),
                'size': f.get('size'),
            }
            for f in files
        ]
        return documents, None
    except HttpError as e:
        return None, str(e)


def fetch_user_profile(request):
    creds = get_credentials(request)
    if not creds:
        return None
    service = build('oauth2', 'v2', credentials=creds, cache_discovery=False)
    try:
        info = service.userinfo().get().execute()
        return {
            'email': info.get('email'),
            'name': info.get('name'),
            'picture': info.get('picture'),
        }
    except HttpError:
        return None
