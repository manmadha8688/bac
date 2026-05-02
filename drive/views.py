from urllib.parse import urlencode

from django.conf import settings
from django.http import HttpResponseRedirect, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .document_qa import parse_chat_body, run_document_qa
from .drive_service import fetch_user_profile, list_files_in_folder, list_folders
from .google_oauth import authorization_url, clear_session, exchange_code


def _json_error(message, status=400):
    return JsonResponse({'detail': message}, status=status)


def _frontend(path, **query):
    base = f"{settings.FRONTEND_URL.rstrip('/')}{path}"
    if query:
        return f"{base}?{urlencode(query)}"
    return base


@require_GET
def auth_google_start(request):
    """Return Google OAuth URL for the SPA to redirect the browser to."""
    try:
        url = authorization_url(request)
    except RuntimeError as e:
        return _json_error(str(e), 500)
    return JsonResponse({'authorization_url': url})


@require_GET
def auth_google_callback(request):
    """OAuth redirect target (configure this exact URL in Google Cloud Console)."""
    error = request.GET.get('error')
    if error:
        return HttpResponseRedirect(_frontend('/login', error=error))
    code = request.GET.get('code')
    state = request.GET.get('state')
    if not code or not state:
        return HttpResponseRedirect(_frontend('/login', error='missing_code'))
    try:
        exchange_code(request, code, state)
    except ValueError:
        return HttpResponseRedirect(_frontend('/login', error='invalid_state'))
    except Exception:
        return HttpResponseRedirect(_frontend('/login', error='token_exchange'))
    return HttpResponseRedirect(_frontend('/dashboard'))


@require_GET
def auth_me(request):
    profile = fetch_user_profile(request)
    if not profile or not profile.get('email'):
        return _json_error('Not authenticated', 401)
    return JsonResponse({'user': profile})


@csrf_exempt
@require_POST
def auth_logout(request):
    clear_session(request)
    return JsonResponse({'ok': True})


@require_GET
def folders_list(request):
    folders, err = list_folders(request)
    if err == 'not_authenticated':
        return _json_error('Not authenticated', 401)
    if err:
        return _json_error(err, 502)
    return JsonResponse({'folders': folders})


@require_GET
def documents_list(request):
    folder_id = request.GET.get('folder_id') or request.GET.get('folderId')
    if not folder_id:
        return _json_error('folder_id is required')
    docs, err = list_files_in_folder(request, folder_id)
    if err == 'not_authenticated':
        return _json_error('Not authenticated', 401)
    if err:
        return _json_error(err, 502)
    return JsonResponse({'documents': docs})


@csrf_exempt
@require_POST
def chat(request):
    """Aggregate folder document text + LLM answer (OPENAI_API_KEY)."""
    body = parse_chat_body(request.body)
    question = body.get('question') or ''
    folder_id = body.get('folder_id') or ''
    conversation = body.get('conversation')
    google_access_token = body.get('google_access_token')

    result = run_document_qa(
        request=request,
        folder_id=str(folder_id).strip(),
        question=str(question).strip(),
        conversation=conversation if isinstance(conversation, list) else [],
        google_access_token=google_access_token
        if isinstance(google_access_token, str)
        else None,
    )

    err = result.get('error')
    if err == 'no_credentials':
        return _json_error(
            result.get('answer') or 'Not authenticated for Drive on the server.',
            401,
        )

    return JsonResponse(
        {
            'answer': result.get('answer') or '',
            'citations': result.get('citations') or [],
        },
    )
