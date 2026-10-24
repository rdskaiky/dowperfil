# app.py — dowPerfil.RDS backend
# Deploy: Render | Start: gunicorn app:app --timeout 120

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
import os
import tempfile
import shutil
import threading
import time
import zipfile
import uuid
import traceback
from pathlib import Path

# ── App + CORS ─────────────────────────────────────────────────
app = Flask(__name__, static_folder=None)
CORS(app)

# ── Cache global ───────────────────────────────────────────────
PROFILES = {}
JOBS = {}
PROFILES_TTL = 1800
JOBS_TTL     = 900


# ── Headers de navegador ───────────────────────────────────────
BROWSER_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/131.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8',
    'Sec-Fetch-Mode': 'navigate',
}


# ── Limpeza automática ─────────────────────────────────────────
def _cleanup_loop():
    while True:
        time.sleep(60)
        now = time.time()
        for k in list(PROFILES.keys()):
            if PROFILES[k]['expires_at'] < now:
                PROFILES.pop(k, None)
        for k in list(JOBS.keys()):
            if JOBS[k]['expires_at'] < now:
                shutil.rmtree(JOBS[k].get('tmpdir', ''), ignore_errors=True)
                JOBS.pop(k, None)

threading.Thread(target=_cleanup_loop, daemon=True).start()


# ── Health check ───────────────────────────────────────────────
@app.route('/')
def index():
    return jsonify({
        'status': 'ok',
        'service': 'dowPerfil.RDS backend',
        'version': '1.2',
        'curl_cffi_available': _check_curl_cffi(),
    })


def _check_curl_cffi():
    try:
        import curl_cffi  # noqa: F401
        return True
    except ImportError:
        return False


# ── Helpers ───────────────────────────────────────────────────
def _slug_from_url(url):
    try:
        path = url.split('?')[0].rstrip('/')
        if '/@' in path:
            return path.split('/@', 1)[1].split('/')[0]
    except Exception:
        pass
    return None


def _normalize_video(entry):
    return {
        'id': entry.get('id') or str(uuid.uuid4())[:12],
        'title': (entry.get('title') or entry.get('description') or 'Vídeo do TikTok')[:200],
        'thumbnail': entry.get('thumbnail') or '',
        'duration': entry.get('duration') or 0,
        'webpage_url': entry.get('webpage_url') or '',
    }


def _base_opts(limit=None, flat=False):
    opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'extract_flat': flat,
        'http_headers': BROWSER_HEADERS,
        'socket_timeout': 30,
        'retries': 3,
        'fragment_retries': 3,
    }
    if limit:
        opts['playlistend'] = limit
    return opts


def _ydl_opts(limit=None, flat=False):
    """Opções com impersonate; quem chamar trata fallback."""
    opts = _base_opts(limit=limit, flat=flat)
    opts['impersonate'] = 'chrome'
    return opts


def _extract_info_with_fallback(url, limit=None, flat=False):
    """
    Tenta extrair com impersonate='chrome'.
    Fallback sem impersonate se curl_cffi faltar ou o servidor rejeitar.
    """
    try:
        with yt_dlp.YoutubeDL(_ydl_opts(limit=limit, flat=flat)) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e1:
        err_str = str(e1).lower()
        # Impersonate falhou? Tenta sem.
        if ('impersonate' in err_str or 'curl_cffi' in err_str
                or 'curl-cffi' in err_str or 'target' in err_str):
            print(f'[fallback] impersonate falhou ({type(e1).__name__}: {e1})')
            with yt_dlp.YoutubeDL(_base_opts(limit=limit, flat=flat)) as ydl:
                return ydl.extract_info(url, download=False)
        raise


def _download_with_fallback(webpage_url, outtmpl, audio=False):
    def _build_opts(use_impersonate):
        opts = {
            'quiet': True,
            'no_warnings': True,
            'outtmpl': outtmpl,
            'http_headers': BROWSER_HEADERS,
            'socket_timeout': 30,
            'retries': 3,
            'fragment_retries': 3,
        }
        if use_impersonate:
            opts['impersonate'] = 'chrome'
        if audio:
            opts['format'] = 'bestaudio/best'
            opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
            }]
        else:
            opts['format'] = 'best[ext=mp4]/best'
        return opts

    try:
        with yt_dlp.YoutubeDL(_build_opts(True)) as ydl:
            ydl.download([webpage_url])
    except Exception as e1:
        err_str = str(e1).lower()
        if ('impersonate' in err_str or 'curl_cffi' in err_str
                or 'curl-cffi' in err_str or 'target' in err_str):
            print(f'[fallback] download sem impersonate ({type(e1).__name__}: {e1})')
            with yt_dlp.YoutubeDL(_build_opts(False)) as ydl:
                ydl.download([webpage_url])
        else:
            raise


def _err_message(e, prefix):
    """Retorna uma string amigável, mesmo quando str(e) é vazia."""
    detail = str(e).strip() or f'{type(e).__name__} (sem mensagem)'
    low = detail.lower()
    if 'unexpected response' in low or '403' in low or '429' in low:
        return (
            f'{prefix}: o TikTok bloqueou o acesso do servidor. '
            f'Tente de novo em alguns minutos. Detalhe: {detail}'
        )
    return f'{prefix}: {detail}'


# ── API: resolver perfil ──────────────────────────────────────
@app.route('/api/profile', methods=['POST'])
def api_profile():
    data = request.get_json() or {}
    url = (data.get('url') or '').strip()
    try:
        limit = max(1, min(int(data.get('limit') or 20), 100))
    except (TypeError, ValueError):
        limit = 20

    if not url:
        return jsonify({'error': 'Cole o link do perfil.'}), 400

    try:
        # Para PERFIL, extract_flat='in_playlist' é muito mais leve e
        # evita que o yt-dlp busque cada vídeo individualmente (anti-bot).
        info = _extract_info_with_fallback(url, limit=limit, flat='in_playlist')

        if not info:
            return jsonify({'error': 'Perfil não encontrado.'}), 404

        is_playlist = info.get('_type') == 'playlist'
        entries_raw = info.get('entries') if is_playlist else [info]

        # Força a iteração da LazyList com contexto para logar erros reais
        entries = []
        try:
            for e in (entries_raw or []):
                if e:
                    entries.append(e)
                if len(entries) >= limit:
                    break
        except Exception as lazy_err:
            print(f'[lazy-list-error] {type(lazy_err).__name__}: {lazy_err}')
            traceback.print_exc()
            return jsonify({
                'error': (
                    'O TikTok retornou uma resposta inesperada ao listar os vídeos. '
                    'Tente novamente em alguns minutos. '
                    f'Detalhe: {type(lazy_err).__name__}'
                )
            }), 502

        if not entries:
            return jsonify({
                'error': 'Nenhum vídeo público encontrado neste perfil.'
            }), 404

        # Normaliza; completa com webpage_url usando o username se faltar
        username = _slug_from_url(url) or ''
        videos = []
        for e in entries:
            v = _normalize_video(e)
            if not v['webpage_url'] and username and v['id']:
                v['webpage_url'] = f'https://www.tiktok.com/@{username}/video/{v["id"]}'
            videos.append(v)

        profile = {
            'username': info.get('uploader_id') or info.get('channel') or username or 'perfil',
            'display_name': info.get('uploader') or info.get('channel') or '',
            'avatar': info.get('channel_thumbnail') or '',
            'count': len(videos),
            'source_url': url,
        }

        slug = str(uuid.uuid4())[:12]
        PROFILES[slug] = {
            'info': profile,
            'videos': videos,
            'expires_at': time.time() + PROFILES_TTL,
        }

        return jsonify({
            'session_id': slug,
            'profile': profile,
            'videos': [
                {**v, 'videoUrl': f'/api/download/{slug}/{v["id"]}?format=video'}
                for v in videos
            ],
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': _err_message(e, 'Falha ao ler perfil')}), 500


# ── API: resolver vídeo único ─────────────────────────────────
@app.route('/api/resolve', methods=['POST'])
def api_resolve():
    data = request.get_json() or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'Cole o link do vídeo.'}), 400

    try:
        info = _extract_info_with_fallback(url, flat=False)

        if not info:
            return jsonify({'error': 'Vídeo não encontrado.'}), 404

        video = _normalize_video(info)
        profile = {
            'username': info.get('uploader_id') or info.get('uploader') or '',
            'display_name': info.get('uploader') or '',
            'avatar': info.get('channel_thumbnail') or '',
            'count': 1,
            'source_url': url,
        }

        slug = str(uuid.uuid4())[:12]
        PROFILES[slug] = {
            'info': profile,
            'videos': [video],
            'expires_at': time.time() + PROFILES_TTL,
        }

        return jsonify({
            'session_id': slug,
            'profile': profile,
            'videos': [{**video, 'videoUrl': f'/api/download/{slug}/{video["id"]}?format=video'}],
            'single': True,
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': _err_message(e, 'Falha ao ler vídeo')}), 500


# ── API: download de um arquivo ───────────────────────────────
@app.route('/api/download/<slug>/<vid>')
def api_download_one(slug, vid):
    profile = PROFILES.get(slug)
    if not profile:
        return jsonify({'error': 'Sessão expirada. Carregue o perfil novamente.'}), 404

    video = next((v for v in profile['videos'] if v['id'] == vid), None)
    if not video:
        return jsonify({'error': 'Vídeo não encontrado.'}), 404

    if not video['webpage_url']:
        return jsonify({'error': 'URL do vídeo não disponível.'}), 400

    fmt = request.args.get('format', 'video')
    tmpdir = tempfile.mkdtemp(prefix='dowp_')

    try:
        _download_with_fallback(
            webpage_url=video['webpage_url'],
            outtmpl=os.path.join(tmpdir, '%(id)s.%(ext)s'),
            audio=(fmt == 'audio'),
        )

        files = [f for f in Path(tmpdir).iterdir() if f.is_file()]
        if not files:
            return jsonify({'error': 'Download falhou no servidor.'}), 500

        f = files[0]
        return send_file(str(f), as_attachment=True, download_name=f.name)

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': _err_message(e, 'Falha no download')}), 500
    finally:
        def _clean():
            time.sleep(30)
            shutil.rmtree(tmpdir, ignore_errors=True)
        threading.Thread(target=_clean, daemon=True).start()


# ── API: iniciar batch ────────────────────────────────────────
@app.route('/api/batch/start', methods=['POST'])
def api_batch_start():
    data = request.get_json() or {}
    slug = data.get('session_id')
    ids = data.get('ids') or []

    if not slug or slug not in PROFILES:
        return jsonify({'error': 'Sessão expirada.'}), 404
    if not ids:
        return jsonify({'error': 'Nenhum vídeo selecionado.'}), 400

    profile = PROFILES[slug]
    videos = [v for v in profile['videos'] if v['id'] in ids and v['webpage_url']]
    if not videos:
        return jsonify({'error': 'Vídeos não encontrados.'}), 404

    job_id = str(uuid.uuid4())[:12]
    tmpdir = tempfile.mkdtemp(prefix='dowp_batch_')

    JOBS[job_id] = {
        'status': 'running',
        'total': len(videos),
        'done': 0,
        'current': '',
        'zip_path': None,
        'tmpdir': tmpdir,
        'error': None,
        'expires_at': time.time() + JOBS_TTL,
    }

    def _worker():
        job = JOBS[job_id]
        try:
            for v in videos:
                job['current'] = v['title'][:60]
                try:
                    _download_with_fallback(
                        webpage_url=v['webpage_url'],
                        outtmpl=os.path.join(tmpdir, f'{v["id"]}.%(ext)s'),
                        audio=False,
                    )
                except Exception as e:
                    print(f'[skip {v["id"]}] {type(e).__name__}: {e}')
                job['done'] += 1

            files = [f for f in Path(tmpdir).iterdir() if f.is_file()]
            if not files:
                job['status'] = 'error'
                job['error'] = 'Nenhum arquivo foi baixado.'
                return

            if len(files) == 1:
                job['zip_path'] = str(files[0])
                job['single_file'] = True
            else:
                zip_path = os.path.join(tmpdir, 'dowPerfil_RDS.zip')
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for f in files:
                        zf.write(f, f.name)
                job['zip_path'] = zip_path
                job['single_file'] = False

            job['status'] = 'done'
            job['current'] = ''

        except Exception as e:
            traceback.print_exc()
            job['status'] = 'error'
            job['error'] = str(e) or f'{type(e).__name__}: erro desconhecido'

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({'job_id': job_id, 'total': len(videos)})


# ── API: status do batch ──────────────────────────────────────
@app.route('/api/batch/status/<job_id>')
def api_batch_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({'error': 'Job expirado.'}), 404
    return jsonify({
        'status': job['status'],
        'total': job['total'],
        'done': job['done'],
        'current': job['current'],
        'error': job['error'],
    })


# ── API: baixar o ZIP pronto ──────────────────────────────────
@app.route('/api/batch/download/<job_id>')
def api_batch_download(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({'error': 'Job expirado.'}), 404
    if job['status'] != 'done':
        return jsonify({'error': 'Ainda processando.'}), 409

    name = 'dowPerfil_RDS.zip' if not job.get('single_file') else 'video.mp4'
    return send_file(job['zip_path'], as_attachment=True, download_name=name)


# ── Entry point ───────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
