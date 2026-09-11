import os
import sys
import json
import time
import sqlite3
import secrets
import re
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    sys.stdout.reconfigure(encoding='ascii', errors='replace')
    sys.stderr.reconfigure(encoding='ascii', errors='replace')
except Exception:
    pass

# Read .env without external packages.
def load_env(path='/home/container/.env'):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"\''))
    except FileNotFoundError:
        pass

load_env()

BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
CHANNEL_USERNAME = os.getenv('CHANNEL_USERNAME', '@Atal7138').strip()
ADMIN_ID_RAW = os.getenv('ADMIN_ID', '').strip()
BOT_USERNAME = os.getenv('BOT_USERNAME', 'ATALProduction_Faiz_Bot').strip().lstrip('@')

if not BOT_TOKEN:
    raise RuntimeError('BOT_TOKEN is missing in .env')
if not ADMIN_ID_RAW.isdigit():
    raise RuntimeError('ADMIN_ID must be numeric in .env')
ADMIN_ID = int(ADMIN_ID_RAW)

DB = '/home/container/movies.db'
conn = sqlite3.connect(DB, check_same_thread=False)
conn.execute('CREATE TABLE IF NOT EXISTS movies (code TEXT PRIMARY KEY, file_id TEXT NOT NULL, file_type TEXT NOT NULL, caption TEXT, thumbnail_id TEXT)')
try:
    conn.execute('ALTER TABLE movies ADD COLUMN thumbnail_id TEXT')
    conn.commit()
except sqlite3.OperationalError:
    pass

pending_admin = {}

def tg(method, data=None):
    url = 'https://api.telegram.org/bot' + BOT_TOKEN + '/' + method
    body = urllib.parse.urlencode(data or {}).encode('utf-8')
    req = urllib.request.Request(url, data=body, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=70) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        print('API error [' + method + ']: ' + str(e))
        return {'ok': False, 'description': str(e)}

def send_message(chat_id, text, reply_markup=None):
    data = {
        'chat_id': chat_id,
        'text': text,
        'disable_web_page_preview': 'true'
    }
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup, ensure_ascii=False)
    return tg('sendMessage', data)

def send_photo(chat_id, photo_id, caption=''):
    return tg('sendPhoto', {'chat_id': chat_id, 'photo': photo_id, 'caption': caption or ''})

def user_name(user):
    first = (user.get('first_name') or '').strip()
    last = (user.get('last_name') or '').strip()
    username = (user.get('username') or '').strip()
    name = (first + ' ' + last).strip()
    return name or (('@' + username) if username else 'ملګري')

def join_keyboard(code=''):
    callback = 'joined'
    if code:
        callback = 'joined:' + code
    return {
        'inline_keyboard': [
            [{'text': '📢 زموږ چینل سره یوځای شه', 'url': 'https://t.me/' + CHANNEL_USERNAME.lstrip('@')}],
            [{'text': 'یوځای سوم👍', 'callback_data': callback}]
        ]
    }

def is_member(user_id):
    r = tg('getChatMember', {'chat_id': CHANNEL_USERNAME, 'user_id': user_id})
    if not r.get('ok'):
        return False
    status = r.get('result', {}).get('status', '')
    return status in ('creator', 'administrator', 'member')

def get_movie(code):
    return conn.execute(
        'SELECT file_id, file_type, caption, thumbnail_id FROM movies WHERE code=?',
        (code,)
    ).fetchone()

def deliver(chat_id, code):
    row = get_movie(code)
    if not row:
        send_message(chat_id, '❌ بښنه، د فلم لینک پیدا نه شو.')
        return
    file_id, file_type, caption, thumbnail_id = row

    # For videos: custom poster becomes the video's thumbnail and
    # the saved text appears directly underneath the video.
    if file_type == 'video':
        data = {
            'chat_id': chat_id,
            'video': file_id,
            'caption': caption or ''
        }
        if thumbnail_id:
            data['cover'] = thumbnail_id
        tg('sendVideo', data)
    else:
        # Documents cannot use a custom video thumbnail in this flow.
        tg('sendDocument', {
            'chat_id': chat_id,
            'document': file_id,
            'caption': caption or ''
        })

def new_code():
    while True:
        code = secrets.token_urlsafe(6).replace('-', '').replace('_', '')[:8]
        if not get_movie(code):
            return code

def format_size(size):
    if not size:
        return 'نامعلوم'
    if size >= 1024 ** 3:
        return f'{size / (1024 ** 3):.2f} GB'
    return f'{size / (1024 ** 2):.1f} MB'

def detect_quality(media, caption=''):
    # Prefer Telegram's actual video dimensions.
    height = media.get('height') or 0
    width = media.get('width') or 0
    h = max(height, width)

    if h >= 1900:
        return '1080p'
    if h >= 1500:
        return '900p'
    if h >= 1100:
        return '720p'
    if h >= 700:
        return '480p'

    # For documents or files without dimensions, look in caption/name.
    text = caption or ''
    for q in ('2160p', '1080p', '900p', '720p', '480p', '360p'):
        if re.search(r'(?i)(?<!\d)' + re.escape(q) + r'(?!\d)', text):
            return q
    return 'Download'

def movie_button(quality, size, link):
    return {
        'inline_keyboard': [
            [{'text': f'📥 {quality} 「✦ {size} ✦」', 'url': link}]
        ]
    }

def save_admin_movie(chat_id, media, file_type, caption, thumbnail_id=''):
    # Save first, then notify admin. This prevents a successful upload from
    # being lost just because the confirmation message temporarily fails.
    file_id = media.get('file_id')
    if not file_id:
        send_message(chat_id, '❌ د فلم file_id پیدا نه شو. مهرباني وکړه فلم بیا راولېږه.')
        return

    code = new_code()
    try:
        conn.execute(
            'INSERT INTO movies(code,file_id,file_type,caption,thumbnail_id) VALUES(?,?,?,?,?)',
            (code, file_id, file_type, caption or '', thumbnail_id or None)
        )
        conn.commit()
    except Exception as e:
        print('DB save error: ' + str(e))
        send_message(chat_id, '❌ فلم ثبت نه شو. بیا یې راولېږه.')
        return

    link = 'https://t.me/' + BOT_USERNAME + '?start=' + code
    quality = detect_quality(media, caption)
    size = format_size(media.get('file_size'))
    text_out = (
        '🎬 فلم تیار شو! ✅\n\n'
        f'کیفیت: {quality}\n'
        f'سایز: {size}\n\n'
        '👇 دا بټن خپل Preview پوسټ کې کېږده؛ اصلي اوږد لینک به نه ښکاري.'
    )
    markup = movie_button(quality, size, link)

    # Confirmation retry: Telegram/network hiccups should not make it look
    # as if the upload was lost.
    for attempt in range(3):
        result = send_message(chat_id, text_out, markup)
        if result.get('ok'):
            return
        time.sleep(2)

    print('Confirmation message failed after 3 attempts. Movie code: ' + code)


def handle_message(m):
    chat = m.get('chat', {})
    user = m.get('from', {})
    chat_id = chat.get('id')
    user_id = user.get('id')
    text = m.get('text', '') or ''

    if text.startswith('/id'):
        send_message(chat_id, '🆔 ستا Telegram ID:\n\n' + str(user_id))
        return

    if text.startswith('/start'):
        parts = text.split(maxsplit=1)
        code = parts[1].strip() if len(parts) > 1 else ''

        if not is_member(user_id):
            name = user_name(user)
            msg = (
                f'ګرانه {name} ❤️\n\n'
                'د روباټ څخه د استفادې کولو لپاره مهرباني وکړه '
                'زموږ د لاندې چینل سره یوځای شه.! 🙂\n\n'
                'وروسته د «یوځای سوم👍» بټنه ووهه.!'
            )
            send_message(chat_id, msg, join_keyboard(code))
            return

        if code:
            deliver(chat_id, code)
        else:
            name = user_name(user)
            send_message(
                chat_id,
                f'سلام {name} ❤️\nد فلمونو لپاره زموږ چینل سره یوځای شه.',
                join_keyboard()
            )
        return

    # ADMIN: send thumbnail photo + caption first, then send the movie.
    if user_id == ADMIN_ID and m.get('photo'):
        photo = m['photo'][-1]
        pending_admin[chat_id] = {
            'thumbnail_id': photo['file_id'],
            'caption': m.get('caption') or ''
        }
        send_message(
            chat_id,
            '✅ عکس او لیکنه ثبت شوه.\n\n'
            'اوس هماغه فلم Video یا File په بل پیغام کې راولېږه. 🎬'
        )
        return

    # ADMIN: save movie and attach the pending thumbnail/caption if present.
    if user_id == ADMIN_ID and (m.get('video') or m.get('document')):
        if m.get('video'):
            media = m['video']
            file_type = 'video'
        else:
            media = m['document']
            file_type = 'document'

        # Telegram has finished uploading the media and delivered the message
        # to the bot at this point. Do not download the 1.82GB file; only store
        # its Telegram file_id.
        print('Admin media received: type=' + file_type + ', file_id=' + str(media.get('file_id', ''))[:20])

        pending = pending_admin.pop(chat_id, None)
        if pending:
            caption = pending.get('caption', '')
            thumbnail_id = pending.get('thumbnail_id', '')
        else:
            caption = m.get('caption') or ''
            thumbnail_id = ''

        save_admin_movie(chat_id, media, file_type, caption, thumbnail_id)
        return

    if user_id != ADMIN_ID and not is_member(user_id):
        send_message(
            chat_id,
            f'ګرانه {user_name(user)} ❤️\nمهرباني وکړه لومړی زموږ چینل سره یوځای شه.',
            join_keyboard()
        )

def handle_callback(q):
    qid = q.get('id')
    user = q.get('from', {})
    user_id = user.get('id')
    data = q.get('data', '')
    msg = q.get('message', {}) or {}
    chat_id = (msg.get('chat') or {}).get('id')

    tg('answerCallbackQuery', {'callback_query_id': qid})

    if data == 'joined':
        code = ''
    elif data.startswith('joined:'):
        code = data.split(':', 1)[1]
    else:
        return

    if not is_member(user_id):
        if chat_id:
            send_message(
                chat_id,
                f'❌ {user_name(user)}، لا هم چینل ته Join نه یې کړی.\n'
                'لومړی Join کړه، بیا «یوځای سوم👍» ووهه.',
                join_keyboard(code)
            )
        return

    if chat_id:
        if code:
            deliver(chat_id, code)
        else:
            send_message(
                chat_id,
                f'✅ تایید شو، {user_name(user)}! اوس کولی شې فلم ترلاسه کړې.'
            )

class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ('/', '/health'):
            body = b'OK'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != '/telegram':
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            raw = self.rfile.read(length)
            upd = json.loads(raw.decode('utf-8'))
            if 'message' in upd:
                handle_message(upd['message'])
            elif 'callback_query' in upd:
                handle_callback(upd['callback_query'])
            self.send_response(200)
            self.end_headers()
        except Exception as e:
            print('Webhook handler error: ' + str(e))
            self.send_response(200)
            self.end_headers()

    def log_message(self, format, *args):
        return


def main():
    me = tg('getMe')
    if not me.get('ok'):
        raise RuntimeError('BOT_TOKEN is invalid')

    webhook_base = os.getenv('WEBHOOK_URL', '').strip().rstrip('/')
    if not webhook_base:
        raise RuntimeError('WEBHOOK_URL is missing')

    port = int(os.getenv('PORT', '10000'))
    webhook_url = webhook_base + '/telegram'
    result = tg('setWebhook', {
        'url': webhook_url,
        'allowed_updates': json.dumps(['message', 'callback_query'])
    })
    if not result.get('ok'):
        raise RuntimeError('Could not set Telegram webhook: ' + str(result.get('description', 'unknown error')))

    print('AtalBot webhook is running...')
    print('Webhook: ' + webhook_url)
    print('Port: ' + str(port))

    server = HTTPServer(('0.0.0.0', port), WebhookHandler)
    server.serve_forever()

if __name__ == '__main__':
    main()
