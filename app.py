# app.py — dowPerfil.RDS backend
# Deploy: Render | Start: gunicorn app:app

from flask import Flask, request, jsonify, send_file, send_from_directory
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
        'version': '1.0',
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


def _ydl_opts(limit=None, flat=False):
    opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'extract_flat': flat,
    }
    if limit:
        opts['playlistend'] = limit
    return opts


# ── API: resolver perfil ──────────────────────────────────────
@app.route('/api/profile', methods=['POST'])
def api_profile():
    data = request.get_json() or {}
    url = (data.get('url') or '').strip()
    limit = max(1, min(int(data.get('limit') or 20), 100))

    if not url:
        return jsonify({'error': 'Cole o link do perfil.'}), 400

    try:
        with yt_dlp.YoutubeDL(_ydl_opts(limit=limit)) as ydl:
            info = ydl.extract_info(url, download=False)

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
        return jsonify({'error': f'Falha ao ler perfil: {e}'}), 500


# ── API: resolver vídeo único ─────────────────────────────────
@app.route('/api/resolve', methods=['POST'])
def api_resolve():
    data = request.get_json() or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'Cole o link do vídeo.'}), 400

    try:
        with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)

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
        return jsonify({'error': f'Falha ao ler vídeo: {e}'}), 500


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
        opts = {
            'quiet': True, 'no_warnings': True,
            'outtmpl': os.path.join(tmpdir, '%(id)s.%(ext)s'),
        }
        if fmt == 'audio':
            opts['format'] = 'bestaudio/best'
            opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}]
        else:
            opts['format'] = 'best[ext=mp4]/best'

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([video['webpage_url']])

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
                opts = {
                    'quiet': True, 'no_warnings': True,
                    'format': 'best[ext=mp4]/best',
                    'outtmpl': os.path.join(tmpdir, f'{v["id"]}.%(ext)s'),
                }
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.download([v['webpage_url']])
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
