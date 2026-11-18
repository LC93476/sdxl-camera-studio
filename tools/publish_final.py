import json, os, subprocess, sys, urllib.request
ROOT = r'D:/QUARK/sdxl_camera_angle'
TOKEN = os.environ['GH_TOKEN']
OWNER, NAME = 'LC93476', 'sdxl-camera-studio'
def sh(*a):
    p = subprocess.run(a, cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace')
    print('$', ' '.join(a[:3]), '->', p.returncode, (p.stdout + p.stderr).strip()[:300])
    return p
def api(method, path, payload=None):
    url = 'https://api.github.com' + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Authorization', 'token ' + TOKEN)
    req.add_header('Accept', 'application/vnd.github+json')
    req.add_header('User-Agent', 'camera-studio-publish')
    if data: req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or '{}')
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or '{}')
sh('git', 'init', '-q', '-b', 'main')
sh('git', 'config', 'user.name', 'LC93476')
sh('git', 'config', 'user.email', '34701@users.noreply.github.com')
sh('git', 'add', '-A')
sh('git', 'commit', '-q', '-m', 'feat: SDXL 相机控制节点 + 桌面相机工作台')
files = subprocess.run(['git','ls-files'], cwd=ROOT, capture_output=True, text=True).stdout.split()
bad = [f for f in files if f.lower().endswith(('.jpg','.jpeg','.png','.webp'))]
print('tracked:', len(files), '| images:', bad)
url = 'https://x-access-token:%s@github.com/%s/%s.git' % (TOKEN, OWNER, NAME)
sh('git', 'push', '--force', url, 'HEAD:refs/heads/main')
sh('git', 'remote', 'set-url', 'origin', 'https://github.com/%s/%s.git' % (OWNER, NAME))
st, repo = api('PATCH', '/repos/%s/%s' % (OWNER, NAME), {'private': False, 'description': 'SDXL 相机控制：pos_x/pos_y/pos_z/roll 转提示词权重，ComfyUI 节点 + 本地相机工作台', 'has_issues': True})
print('PATCH private ->', st, 'private=', repo.get('private'), 'html=', repo.get('html_url'))
st, tree = api('GET', '/repos/%s/%s/git/trees/main?recursive=1' % (OWNER, NAME))
paths = [t['path'] for t in tree.get('tree', [])]
print('remote files:', len(paths), 'truncated=', tree.get('truncated'))
print('remote images:', [p for p in paths if p.lower().endswith(('.jpg','.jpeg','.png','.webp'))])
st, cm = api('GET', '/repos/%s/%s/commits?per_page=5' % (OWNER, NAME))
print('remote commits:', [(c['sha'][:7], c['commit']['message'].splitlines()[0]) for c in cm])