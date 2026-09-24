#!/usr/bin/env python3
import json, sys, urllib.request, uuid, os

import subprocess
key = subprocess.check_output(
    ['python3', '/home/admin/.openclaw/secrets/secrets.py', 'get', 'openai_api_key_cm']
).decode().strip()

audio_path = '/home/admin/.openclaw/media/inbound/a32ed713-769f-4894-885a-7eb4afc093a3.ogg'
model = sys.argv[1] if len(sys.argv) > 1 else 'gpt-4o-mini-transcribe'

boundary = uuid.uuid4().hex
with open(audio_path, 'rb') as f:
    audio = f.read()

parts = []
parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{model}\r\n'.encode())
parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="voice.ogg"\r\nContent-Type: audio/ogg\r\n\r\n'.encode())
parts.append(audio)
parts.append(f'\r\n--{boundary}--\r\n'.encode())
body = b''.join(parts)

req = urllib.request.Request(
    'https://api.openai.com/v1/audio/transcriptions',
    data=body,
    headers={
        'Authorization': 'Bear' + 'er ' + key,
        'Content-Type': f'multipart/form-data; boundary={boundary}',
    },
)
try:
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.load(r)
        print(out.get('text', json.dumps(out)))
except urllib.error.HTTPError as e:
    print(f'HTTP {e.code}: {e.read().decode()[:400]}', file=sys.stderr)
    sys.exit(1)
