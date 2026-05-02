"""
Load text from Drive folder + answer with OpenAI (env-configured).
"""
from __future__ import annotations

import io
import json
import logging
import re

import requests
from django.conf import settings
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from .google_oauth import get_credentials

logger = logging.getLogger(__name__)

# Strip common secret-shaped strings from citations / UI payloads (documents may contain pasted keys).
_REDACT_RULES = (
    (
        re.compile(r'sk-(?:live|proj|test)[a-zA-Z0-9_-]{8,}', re.I),
        'sk-[REDACTED]',
    ),
    (re.compile(r'sk-[a-zA-Z0-9_-]{24,}', re.I), 'sk-[REDACTED]'),
    (re.compile(r'AIza[0-9A-Za-z_-]{20,}', re.I), '[REDACTED_GOOGLE_API_KEY]'),
    (re.compile(r'GOCSPX-[a-zA-Z0-9_-]{8,}', re.I), '[REDACTED_OAUTH_SECRET]'),
    (re.compile(r'Bearer\s+ey[J-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]+){2,}', re.I), 'Bearer [REDACTED]'),
)


def redact_secrets_display(text: str | None, max_len: int | None = None) -> str:
    if not text:
        return ''
    out = str(text)
    for pat, repl in _REDACT_RULES:
        out = pat.sub(repl, out)
    out = out.replace('\x00', '')
    if max_len is not None and len(out) > max_len:
        out = out[:max_len] + '…'
    return out


def _sanitize_citation_item(c: dict) -> dict:
    return {
        'document_id': c.get('document_id', ''),
        'title': c.get('title', ''),
        'snippet': redact_secrets_display(c.get('snippet'), max_len=500),
    }


MAX_FILES = getattr(settings, 'QA_MAX_DRIVE_FILES', 30)
MAX_CONTEXT_CHARS = getattr(settings, 'QA_MAX_CONTEXT_CHARS', 120_000)
MAX_CHARS_PER_FILE = getattr(settings, 'QA_MAX_CHARS_PER_FILE', 35_000)
MAX_PDF_PAGES = getattr(settings, 'QA_MAX_PDF_PAGES', 40)


def _drive_service(bearer_token: str | None, request):
    if bearer_token:
        try:
            creds = Credentials(token=bearer_token)
            return build('drive', 'v3', credentials=creds, cache_discovery=False)
        except Exception:
            logger.exception('Failed to build Drive client from bearer token')
            return None
    creds = get_credentials(request)
    if not creds:
        return None
    return build('drive', 'v3', credentials=creds, cache_discovery=False)


def _list_files(service, folder_id: str) -> list[dict]:
    safe = folder_id.replace("'", "\\'")
    q = f"'{safe}' in parents and trashed=false"
    resp = (
        service.files()
        .list(
            q=q,
            pageSize=MAX_FILES,
            fields='files(id, name, mimeType, modifiedTime, size)',
            orderBy='folder,name_natural',
        )
        .execute()
    )
    return resp.get('files', [])


def _export_text(service, file_id: str, mime_out: str) -> str | None:
    try:
        req = service.files().export_media(fileId=file_id, mimeType=mime_out)
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = dl.next_chunk()
        raw = buf.getvalue()
        return raw.decode('utf-8', errors='replace')
    except HttpError as e:
        logger.warning('Drive export failed for %s: %s', file_id, e)
        return None


def _download_bytes(service, file_id: str) -> bytes | None:
    try:
        req = service.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = dl.next_chunk()
        return buf.getvalue()
    except HttpError as e:
        logger.warning('Drive download failed for %s: %s', file_id, e)
        return None


def _text_from_pdf(data: bytes) -> str | None:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        r = PdfReader(io.BytesIO(data))
        chunks = []
        for i, page in enumerate(r.pages[:MAX_PDF_PAGES]):
            t = page.extract_text() or ''
            chunks.append(t)
        return '\n'.join(chunks)
    except Exception as e:
        logger.warning('PDF parse failed: %s', e)
        return None


def _extract_file_text(service, meta: dict) -> str | None:
    fid = meta['id']
    name = meta.get('name') or fid
    mime = meta.get('mimeType') or ''

    if mime == 'application/vnd.google-apps.folder':
        return None
    if mime.startswith('video/') or mime.startswith('image/'):
        return None

    sz = meta.get('size')
    try:
        if sz and int(sz) > 30 * 1024 * 1024:
            return None
    except (TypeError, ValueError):
        pass

    body: str | None = None

    if mime == 'application/vnd.google-apps.document':
        body = _export_text(service, fid, 'text/plain')
    elif mime == 'application/vnd.google-apps.spreadsheet':
        body = _export_text(service, fid, 'text/csv')
    elif mime == 'application/vnd.google-apps.presentation':
        body = _export_text(service, fid, 'text/plain')
    elif mime in ('text/plain', 'text/markdown', 'text/csv', 'application/json'):
        raw = _download_bytes(service, fid)
        if raw:
            body = raw.decode('utf-8', errors='replace')
    elif mime == 'application/pdf':
        raw = _download_bytes(service, fid)
        if raw:
            body = _text_from_pdf(raw)

    if not body:
        return None
    body = body.strip()
    if not body:
        return None
    if len(body) > MAX_CHARS_PER_FILE:
        body = body[:MAX_CHARS_PER_FILE] + '\n… [truncated]'
    return f'<<<FILE name="{name}" id="{fid}" mime="{mime}">>>\n{body}'


def build_folder_context(service, folder_id: str) -> tuple[str, list[dict]]:
    """Returns (combined context string for LLM, list of source metadata)."""
    files = _list_files(service, folder_id)
    chunks: list[str] = []
    sources: list[dict] = []
    total = 0

    for f in files:
        if total >= MAX_CONTEXT_CHARS:
            break
        try:
            block = _extract_file_text(service, f)
            if not block:
                continue
        except Exception:
            logger.warning('Skipping file parse %s', f.get('name'), exc_info=True)
            continue
        if total + len(block) > MAX_CONTEXT_CHARS:
            room = MAX_CONTEXT_CHARS - total
            if room < 200:
                break
            block = block[:room] + '\n… [truncated]'
        chunks.append(block)
        sources.append(
            {'document_id': f['id'], 'title': f.get('name', f['id']), 'mime': f.get('mimeType', '')},
        )
        total += len(block)

    return '\n\n---\n\n'.join(chunks), sources


def _build_messages(question: str, conversation: list, context_block: str) -> list[dict]:
    sys = """You are a careful assistant answering ONLY from DOCUMENT CONTEXT below.
Rules:
- If the answer is not contained in or clearly implied by the context, say you cannot find it in the provided documents (do not guess from general knowledge beyond trivial glosses).
- Name which document(s) you used when helpful (exact filenames).
- Never copy secrets from the documents: passwords, API keys (e.g. sk-..., AIza...), OAuth client secrets, or bearer tokens — summarize without quoting them if relevant.
- Be concise."""

    usr = (
        f'DOCUMENT CONTEXT:\n<<<BEGIN>>>\n{context_block}\n<<<END>>>\n\n'
        f'USER QUESTION:\n{question.strip()}'
    )
    msgs: list[dict] = [{'role': 'system', 'content': sys}]
    prior = conversation or []
    tail = prior[-12:]
    for t in tail:
        r = str(t.get('role', '')).strip().lower()
        c = str(t.get('content', '')).strip()
        if r not in ('user', 'assistant') or not c:
            continue
        msgs.append({'role': 'user' if r == 'user' else 'assistant', 'content': c})
    msgs.append({'role': 'user', 'content': usr})
    return msgs


def _guess_citations(answer: str, sources: list[dict]) -> list[dict]:
    out: list[dict] = []
    low = answer.lower()
    seen = set()
    for s in sources:
        name = str(s['title']).lower()
        if name and (name in low or name[: min(40, len(name))] in low):
            key = s['document_id']
            if key in seen:
                continue
            seen.add(key)
            snip = f'Referenced in assistant answer from “{s["title"]}”.'
            out.append(
                {
                    'document_id': s['document_id'],
                    'title': s['title'],
                    'snippet': redact_secrets_display(snip),
                },
            )
        if len(out) >= 8:
            break
    return [_sanitize_citation_item(x) for x in out[:5]]


def _openai_answer(messages: list[dict]) -> str:
    key = getattr(settings, 'OPENAI_API_KEY', '')
    model = getattr(settings, 'OPENAI_CHAT_MODEL', 'gpt-4o-mini')
    if not key:
        raise RuntimeError('OPENAI_API_KEY not set')

    resp = requests.post(
        'https://api.openai.com/v1/chat/completions',
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        json={'model': model, 'messages': messages, 'temperature': 0.2, 'max_tokens': 4096},
        timeout=120,
    )
    if resp.status_code != 200:
        logger.error('OpenAI error %s %s', resp.status_code, resp.text[:500])
        raise RuntimeError(resp.json().get('error', {}).get('message') or resp.text[:400])
    data = resp.json()
    return str(data['choices'][0]['message']['content']).strip()


def run_document_qa(*, request, folder_id: str, question: str, conversation: list, google_access_token: str | None) -> dict:
    if not folder_id or not str(question).strip():
        return {'answer': '', 'citations': [], 'error': 'folder_id and question are required'}

    service = _drive_service(google_access_token, request)
    if not service:
        return {
            'answer': 'Not authenticated for Drive on the server (pass google_access_token for browser OAuth, or sign in via backend).',
            'citations': [],
            'error': 'no_credentials',
        }

    try:
        context, sources = build_folder_context(service, folder_id)
    except HttpError as e:
        logger.exception('Drive list/read failed')
        return {'answer': '', 'citations': [], 'error': str(e)}

    if not context.strip():
        return {
            'answer': (
                'No usable text found in this folder for Q&A yet. '
                'Supported formats include Google Docs/Sheets/Slides exports, '
                'PDF, CSV, Markdown, plain text, and JSON. '
                'Word (.docx) and other binaries are skipped for now.'
            ),
            'citations': [],
        }

    msgs = _build_messages(question, conversation or [], context)

    try:
        if not getattr(settings, 'OPENAI_API_KEY', '').strip():
            return {
                'answer': (
                    'Document text was loaded from your folder, but no LLM API key was found.\n\n'
                    'Add to **backend/.env** (same folder as manage.py):\n'
                    '  OPENAI_API_KEY=sk-...\n'
                    'Then **restart runserver**. Use a new UTF-8 file with no BOM, or restart '
                    'after saving.'
                ),
                'citations': [
                    {
                        'title': 'Configure LLM',
                        'snippet': 'Do not expose document excerpts here when the model is unavailable.',
                        'document_id': '_config',
                    },
                ],
                'error': 'no_llm_key',
            }
        answer = _openai_answer(msgs)
    except Exception as e:
        logger.exception('LLM call failed')
        return {'answer': '', 'citations': [], 'error': str(e)}

    cites = _guess_citations(answer, sources)

    # If model didn’t cite, attach a short preview (redacted — files may contain secrets)
    if not cites and sources:
        n = sources[0]
        md = next((blk for blk in context.split('\n\n---\n\n') if n['document_id'] in blk), '')
        preview_raw = md[:420] + ('…' if len(md) > 420 else '')
        cites = [
            {
                'document_id': n['document_id'],
                'title': n['title'],
                'snippet': redact_secrets_display(preview_raw, max_len=500),
            },
        ]

    cites = [_sanitize_citation_item(c) for c in cites]

    return {'answer': redact_secrets_display(answer), 'citations': cites}


def parse_chat_body(body_raw: bytes) -> dict:
    try:
        return json.loads(body_raw.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return {}
