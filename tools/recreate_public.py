import json, os, subprocess, time, urllib.request, urllib.error
ROOT = r'D:/QUARK/sdxl_camera_angle'
TOKEN = os.environ['GH_TOKEN']
OWNER, NAME = 'LC93476', 'sdxl-camera-studio'
DESC = 'SDXL 相机控制：pos_x/pos_y/pos_z/roll 转提示词权重，ComfyUI 节点 + 本地相机工作台'
def api(method, path, payload=None, auth=True):
    url = 'https://api.github.com' + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if auth: req.add_header('Authorization', 'token ' + TOKEN)
    req.add_header('Accept', 'application/vnd.github+json')
    req.add_header('User-Agent', 'camera-studio-publish')
    if data: req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or '{}')
def sh(*a):
    p = subprocess.run(a, cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace')
    print('$', ' '.join(x for x in a[:4] if 'x-access' not in x), '->', p.returncode, (p.stdout + p.stderr).strip().splitlines()[-1][:200] if (p.stdout + p.stderr).strip() else '')
    return p
st, _ = api('DELETE', '/repos/%s/%s' % (OWNER, NAME))
print('DELETE repo ->', st)
for i in range(10):
    st, _ = api('GET', '/repos/%s/%s' % (OWNER, NAME), auth=False)
    if st == 404: break
    time.sleep(2)
print('gone check ->', st)
st, repo = api('POST', '/user/repos', {'name': NAME, 'description': DESC, 'private': False, 'has_issues': True, 'auto_init': False})
print('CREATE ->', st, repo.get('html_url'), 'private=', repo.get('private'))
url = 'https://x-access-token:%s@github.com/%s/%s.git' % (TOKEN, OWNER, NAME)
sh('git', 'push', url, 'HEAD:refs/heads/main')
sh('git', 'remote', 'set-url', 'origin', 'https://github.com/%s/%s.git' % (OWNER, NAME))
for sha in ('601f2d9ce00a947a182dde5071b5aeed1079df73', '03eb2f9e437bc8dd185d1bc69f29978ec0f530ae'):
    st, _ = api('GET', '/repos/%s/%s/commits/%s' % (OWNER, NAME, sha), auth=False)
    print('old commit', sha[:7], '->', st)
st, tree = api('GET', '/repos/%s/%s/git/trees/main?recursive=1' % (OWNER, NAME), auth=False)
paths = [t['path'] for t in tree.get('tree', [])]
print('files:', len([p for p in paths if '.' in p.split('/')[-1]]), 'images:', [p for p in paths if p.lower().endswith(('.jpg','.jpeg','.png','.webp'))])
st, cm = api('GET', '/repos/%s/%s/commits?per_page=5' % (OWNER, NAME), auth=False)
print('commits:', [(c['sha'][:7], c['commit']['message'].splitlines()[0]) for c in cm if isinstance(c, dict)])