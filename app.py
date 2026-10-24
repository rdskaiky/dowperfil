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
from pathlib import Path

# ── App + CORS (única instância) ───────────────────────────────
app = Flask(__name__, static_folder=None)
CORS(app)

# ── Cache global ───────────────────────────────────────────────
PROFILES = {}
JOBS = {}
PROFILES_TTL = 1800   # 30 min
JOBS_TTL     = 900    # 15 min


# ── Headers de navegador (reforço contra bloqueio) ────────────
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
        'version': '1.1',
    })


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
    """Opções base, sem impersonate (usado como fallback)."""
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
    """Opções com impersonate; se falhar, quem chamar trata o fallback."""
    opts = _base_opts(limit=limit, flat=flat)
    opts['impersonate'] = 'chrome'
    return opts


def _extract_info_with_fallback(url, limit=None, flat=False):
    """
    Tenta extrair com impersonate='chrome'. Se o ambiente não tiver
    curl_cffi ou o servidor rejeitar, tenta de novo sem impersonate.
    """
    # 1ª tentativa: com impersonate
    try:
        with yt_dlp.YoutubeDL(_ydl_opts(limit=limit, flat=flat)) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e1:
        err_str = str(e1).lower()
        # Só cai no fallback se for erro relacionado a impersonate/curl_cffi
        if 'impersonate' in err_str or 'curl_cffi' in err_str or 'curl-cffi' in err_str:
            print(f'[fallback] impersonate falhou ({e1}); tentando sem ele.')
            with yt_dlp.YoutubeDL(_base_opts(limit=limit, flat=flat)) as ydl:
                return ydl.extract_info(url, download=False)
        # Outros erros: propaga
        raise


def _download_with_fallback(webpage_url, outtmpl, audio=False):
    """
    Baixa um vídeo com impersonate; fallback sem ele se necessário.
    """
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
        if 'impersonate' in err_str or 'curl_cffi' in err_str or 'curl-cffi' in err_str:
            print(f'[fallback] download sem impersonate ({e1})')
            with yt_dlp.YoutubeDL(_build_opts(False)) as ydl:
                ydl.download([webpage_url])
        else:
            raise


# ── API: resolver perfil ──────────────────────────────────────
@app.route('/api/profile', methods=['POST'])
def api_profile():
    data = request.get_json() or {}
    url = (data.get('url') or '').strip()
    limit = max(1, min(int(data.get('limit') or 20), 100))

    if not url:
        return jsonify({'error': 'Cole o link do perfil.'}), 400

    try:
        info = _extract_info_with_fallback(url, limit=limit)

        if not info:
            return jsonify({'error': 'Perfil não encontrado.'}), 404

        is_playlist = info.get('_type') == 'playlist'
        entries = info.get('entries') if is_playlist else [info]
        entries = [e for e in (entries or []) if e][:limit]

        if not entries:
            return jsonify({'error': 'Nenhum vídeo público encontrado neste perfil.'}), 404

        videos = [_normalize_video(e) for e in entries]

        profile = {
            'username': info.get('uploader_id') or info.get('channel') or _slug_from_url(url) or 'perfil',
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
        msg = str(e)
        if 'Unexpected response' in msg or '403' in msg or '429' in msg:
            msg = (
                'O TikTok bloqueou o acesso do servidor. '
                'Tente novamente em alguns minutos. '
                f'Detalhe técnico: {e}'
            )
        return jsonify({'error': f'Falha ao ler perfil: {msg}'}), 500


# ── API: resolver vídeo único ─────────────────────────────────
@app.route('/api/resolve', methods=['POST'])
def api_resolve():
    data = request.get_json() or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'Cole o link do vídeo.'}), 400

    try:
        info = _extract_info_with_fallback(url)

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
        msg = str(e)
        if 'Unexpected response' in msg or '403' in msg or '429' in msg:
            msg = (
                'O TikTok bloqueou o acesso. Tente novamente em alguns minutos. '
                f'Detalhe: {e}'
            )
        return jsonify({'error': f'Falha ao ler vídeo: {msg}'}), 500


# ── API: download de um arquivo ───────────────────────────────
@app.route('/api/download/<slug>/<vid>')
def api_download_one(slug, vid):
    profile = PROFILES.get(slug)
    if not profile:
        return jsonify({'error': 'Sessão expirada. Carregue o perfil novamente.'}), 404

    video = next((v for v in profile['videos'] if v['id'] == vid), None)
    if not video:
        return jsonify({'error': 'Vídeo não encontrado.'}), 404

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
            return jsonify({'error': 'Download falhou.'}), 500

        f = files[0]
        return send_file(str(f), as_attachment=True, download_name=f.name)

    except Exception as e:
        return jsonify({'error': str(e)}), 500
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
    videos = [v for v in profile['videos'] if v['id'] in ids]
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
                    print(f'[skip {v["id"]}] {e}')
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
            job['status'] = 'error'
            job['error'] = str(e)

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
