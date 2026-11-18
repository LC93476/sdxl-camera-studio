import json, os, urllib.request
T = os.environ['GH_TOKEN']
def get(p, raw=False):
    url = ('https://raw.githubusercontent.com/' if raw else 'https://api.github.com/') + p
    req = urllib.request.Request(url)
    if not raw: req.add_header('Authorization', 'token ' + T)
    req.add_header('User-Agent', 'v')
    try:
        with urllib.request.urlopen(req, timeout=60) as r: return r.status, r.read()
    except urllib.error.HTTPError as e: return e.code, b''
R = 'LC93476/sdxl-camera-studio'
st, b = get('repos/' + R)
d = json.loads(b)
print('repo', st, 'private=', d['private'], 'size_KB=', d['size'], 'default=', d['default_branch'], 'html=', d['html_url'])
st, b = get('repos/%s/git/trees/main?recursive=1' % R)
paths = [x['path'] for x in json.loads(b)['tree']]
print('entries:', len(paths))
print('images:', [p for p in paths if p.lower().endswith(('.jpg','.jpeg','.png','.webp'))])
st, b = get('repos/%s/commits?per_page=5' % R)
print('commits:', [(c['sha'][:7], c['commit']['message'].splitlines()[0]) for c in json.loads(b)])
for f in ('README.md', 'docs/screenshots/mapping.png', 'docs/screenshots/ui_overview.png', 'studio/icon.png'):
    st, b = get('%s/main/%s' % (R, f), raw=True)
    print('raw', f, st, len(b))
for sha in ('601f2d9ce00a947a182dde5071b5aeed1079df73',):
    st, b = get('%s/main/docs/screenshots/demo_t2i_1200x1800.jpg' % R, raw=True)
    print('removed demo image ->', st)