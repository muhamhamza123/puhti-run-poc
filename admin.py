"""
Admin panel — GitHub OAuth (DIWA org) + dashboard endpoints.
All routes are prefixed /admin and mounted in main.py.
"""
import os, json, time, hashlib, hmac, contextlib, re, sqlite3
import urllib.request, urllib.parse, urllib.error
from fastapi import APIRouter, HTTPException, Request, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

router = APIRouter(prefix='/admin')

# ── Config ────────────────────────────────────────────────────────────────────

GITHUB_CLIENT_ID      = os.environ.get('GITHUB_CLIENT_ID', '')
GITHUB_CLIENT_SECRET  = os.environ.get('GITHUB_CLIENT_SECRET', '')
GITHUB_ORG            = os.environ.get('GITHUB_ADMIN_ORG', 'DIWA-data-lab')
SESSION_SECRET        = os.environ.get('ADMIN_SESSION_SECRET', 'change-me-in-override')
PUBLIC_URL            = os.environ.get('PUBLIC_URL', 'https://hbv.we3data.com/puhti')

DB_PATH    = os.environ.get('RUN_DB_PATH',   '/data/hbv/runs/runs.db')
NFS_RUNS   = os.environ.get('RUNS_ROOT',     '/data/hbv/runs')
PUHTI_RUNS = os.environ.get('PUHTI_RUNS',    '/scratch/project_2014823/runs')
SSH_KEY    = os.environ.get('PUHTI_SSH_KEY', '/home/hbv/.ssh/id_puhti')
PUHTI_USER = os.environ.get('PUHTI_USER',    'javedham')
PUHTI_HOST = os.environ.get('PUHTI_HOST',    'puhti.csc.fi')

CALLBACK_URL = f'{PUBLIC_URL}/admin/callback'

# ── Session cookie helpers ────────────────────────────────────────────────────

def _sign(value: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()  # hmac.new = hmac.HMAC constructor

def _make_session(username: str) -> str:
    payload = f'{username}:{int(time.time())}'
    return f'{payload}.{_sign(payload)}'

def _verify_session(cookie: str) -> str | None:
    """Return username if cookie is valid, else None."""
    try:
        payload, sig = cookie.rsplit('.', 1)
        if not hmac.compare_digest(_sign(payload), sig):
            return None
        username, ts = payload.rsplit(':', 1)
        if time.time() - int(ts) > 86400 * 7:  # 7-day expiry
            return None
        return username
    except Exception:
        return None

def _require_auth(admin_session: str | None) -> str:
    if not admin_session:
        raise HTTPException(302, headers={'Location': f'{PUBLIC_URL}/admin/login'})
    user = _verify_session(admin_session)
    if not user:
        raise HTTPException(302, headers={'Location': f'{PUBLIC_URL}/admin/login'})
    return user

# ── GitHub OAuth ──────────────────────────────────────────────────────────────

def _gh_api(path: str, token: str) -> dict:
    url = f'https://api.github.com{path}'
    req = urllib.request.Request(url, headers={
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github+json',
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


@router.get('/login')
def login():
    if not GITHUB_CLIENT_ID:
        raise HTTPException(500, 'GITHUB_CLIENT_ID not configured')
    params = urllib.parse.urlencode({
        'client_id': GITHUB_CLIENT_ID,
        'redirect_uri': CALLBACK_URL,
        'scope': 'read:org',
    })
    return RedirectResponse(f'https://github.com/login/oauth/authorize?{params}')


@router.get('/callback')
def callback(code: str = ''):
    if not code:
        raise HTTPException(400, 'Missing OAuth code')

    # Exchange code for token
    data = urllib.parse.urlencode({
        'client_id': GITHUB_CLIENT_ID,
        'client_secret': GITHUB_CLIENT_SECRET,
        'code': code,
        'redirect_uri': CALLBACK_URL,
    }).encode()
    req = urllib.request.Request(
        'https://github.com/login/oauth/access_token', data=data,
        headers={'Accept': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        token_data = json.loads(r.read())
    access_token = token_data.get('access_token', '')
    if not access_token:
        error = token_data.get('error_description', token_data.get('error', 'unknown'))
        raise HTTPException(401, f'GitHub OAuth failed: {error}')

    # Get username
    user_data = _gh_api('/user', access_token)
    username = user_data.get('login', '')

    # Check org membership — try membership endpoint first, fall back to allowlist
    ALLOWED_USERS = {'muhamhamza123', 'LizCarter492'}
    if username not in ALLOWED_USERS:
        try:
            membership = _gh_api(f'/user/memberships/orgs/{GITHUB_ORG}', access_token)
            state = membership.get('state', '')
            if state not in ('active', 'pending'):
                raise HTTPException(403, f'User {username!r} is not an active member of {GITHUB_ORG}')
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(403, f'User {username!r} is not authorised to access this panel')

    session = _make_session(username)
    resp = RedirectResponse(f'{PUBLIC_URL}/admin')
    resp.set_cookie('admin_session', session, httponly=True, samesite='lax', max_age=86400*7)
    return resp


@router.get('/logout')
def logout():
    resp = RedirectResponse(f'{PUBLIC_URL}/admin/login')
    resp.delete_cookie('admin_session')
    return resp

# ── SSH helper ────────────────────────────────────────────────────────────────

import subprocess

def _ssh(cmd: str, timeout: int = 30) -> str:
    r = subprocess.run(
        ['ssh', '-i', SSH_KEY, '-o', 'StrictHostKeyChecking=no',
         '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
         f'{PUHTI_USER}@{PUHTI_HOST}', cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    return r.stdout.strip()

# ── Puhti cache ───────────────────────────────────────────────────────────────

_puhti_cache: dict = {}
_puhti_cache_ts: float = 0
_CACHE_TTL = 900  # 15 minutes


def _puhti_data() -> dict:
    global _puhti_cache, _puhti_cache_ts
    if time.time() - _puhti_cache_ts < _CACHE_TTL:
        return _puhti_cache

    data: dict = {}

    # Current queue for project
    try:
        out = _ssh('squeue -A project_2014823 -o "%u|%i|%P|%T|%l|%V" --noheader 2>/dev/null')
        jobs = []
        for line in out.splitlines():
            parts = line.split('|')
            if len(parts) >= 6:
                jobs.append({'user': parts[0], 'job_id': parts[1], 'partition': parts[2],
                             'state': parts[3], 'time_limit': parts[4], 'submit_time': parts[5]})
        data['queue'] = jobs
    except Exception as e:
        data['queue'] = []
        data['queue_error'] = str(e)

    # Partition info
    try:
        out = _ssh('sinfo -o "%P|%a|%D|%C" --noheader 2>/dev/null')
        partitions = []
        for line in out.splitlines():
            parts = line.split('|')
            if len(parts) == 4:
                alloc, idle, other, total = parts[3].split('/') if '/' in parts[3] else ('?','?','?','?')
                partitions.append({'partition': parts[0].rstrip('*'), 'available': parts[1],
                                   'nodes': parts[2], 'cpus_alloc': alloc,
                                   'cpus_idle': idle, 'cpus_total': total})
        data['partitions'] = partitions
    except Exception:
        data['partitions'] = []

    # Disk usage
    try:
        out = _ssh(f'du -sh {PUHTI_RUNS} 2>/dev/null')
        data['scratch_total'] = out.split()[0] if out else '?'
    except Exception:
        data['scratch_total'] = '?'

    # Per-user scratch usage (Puhti has user subdirs)
    try:
        out = _ssh(f'du -sh {PUHTI_RUNS}/*/ 2>/dev/null')
        user_disk = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2:
                user = parts[1].rstrip('/').split('/')[-1]
                # skip UUID-looking entries (job dirs at wrong level)
                if len(user) == 36 and user.count('-') == 4:
                    continue
                user_disk.append({'user': user, 'size': parts[0]})
        data['user_disk'] = user_disk
    except Exception:
        data['user_disk'] = []

    # Billing units
    try:
        out = _ssh('csc-projects --show-billing-units 2>/dev/null | grep -A5 project_2014823')
        data['billing'] = out or 'unavailable'
    except Exception:
        data['billing'] = 'unavailable'

    # Monthly usage per user via sacct
    try:
        from datetime import date
        start = date.today().replace(day=1).strftime('%Y-%m-%d')
        out = _ssh(
            f'sacct -A project_2014823 --starttime={start} --noheader '
            f'--format=User,CPUTimeRAW,ElapsedRaw,State -P 2>/dev/null'
        )
        usage: dict = {}
        for line in out.splitlines():
            parts = line.split('|')
            if len(parts) < 4 or not parts[0]:
                continue
            user = parts[0]
            try:
                cpu_sec = int(parts[1])
            except ValueError:
                continue
            if user not in usage:
                usage[user] = {'cpu_hours': 0, 'jobs': 0}
            usage[user]['cpu_hours'] += cpu_sec / 3600
            usage[user]['jobs'] += 1
        data['monthly_usage'] = [
            {'user': u, 'cpu_hours': round(v['cpu_hours'], 1), 'jobs': v['jobs']}
            for u, v in sorted(usage.items(), key=lambda x: -x[1]['cpu_hours'])
        ]
    except Exception:
        data['monthly_usage'] = []


    # Node health (per-node state, CPUs, memory)
    try:
        out = _ssh('sinfo -N -o "%N|%T|%C|%m|%e" --noheader 2>/dev/null')
        nodes = []
        for line in out.splitlines():
            parts = line.split('|')
            if len(parts) < 5:
                continue
            cpus_str = parts[2]  # alloc/idle/other/total
            alloc, idle, other, total = cpus_str.split('/') if '/' in cpus_str else ('?','?','?','?')
            nodes.append({
                'node': parts[0],
                'state': parts[1],
                'cpus_alloc': alloc, 'cpus_idle': idle, 'cpus_total': total,
                'mem_total_mb': parts[3],
                'mem_free_mb': parts[4],
            })
        data['nodes'] = nodes
    except Exception:
        data['nodes'] = []

    # GPU partition detail
    try:
        out = _ssh('sinfo -p gpu -N -o "%N|%T|%G|%C" --noheader 2>/dev/null')
        gpu_nodes = []
        for line in out.splitlines():
            parts = line.split('|')
            if len(parts) < 4:
                continue
            cpus_str = parts[3]
            alloc, idle, other, total = cpus_str.split('/') if '/' in cpus_str else ('?','?','?','?')
            gpu_nodes.append({
                'node': parts[0], 'state': parts[1],
                'gres': parts[2],
                'cpus_alloc': alloc, 'cpus_total': total,
            })
        data['gpu_nodes'] = gpu_nodes
    except Exception:
        data['gpu_nodes'] = []

    # Pending reasons breakdown
    try:
        out = _ssh('squeue -A project_2014823 -t PD -o "%R" --noheader 2>/dev/null')
        reasons: dict = {}
        for line in out.splitlines():
            r = line.strip()
            if r:
                reasons[r] = reasons.get(r, 0) + 1
        data['pending_reasons'] = [{'reason': k, 'count': v}
                                    for k, v in sorted(reasons.items(), key=lambda x: -x[1])]
    except Exception:
        data['pending_reasons'] = []

    # Job efficiency via sacct (CPU used vs allocated, last 30 days)
    try:
        from datetime import date, timedelta
        start30 = (date.today() - timedelta(days=30)).strftime('%Y-%m-%d')
        out = _ssh(
            f'sacct -A project_2014823 --starttime={start30} --noheader '
            f'--format=User,CPUTimeRAW,TotalCPU,State -P 2>/dev/null'
        )
        eff: dict = {}
        for line in out.splitlines():
            parts = line.split('|')
            if len(parts) < 4 or not parts[0] or parts[3] not in ('COMPLETED','FAILED'):
                continue
            user = parts[0]
            try:
                allocated = int(parts[1])
                # TotalCPU is HH:MM:SS
                tc = parts[2]
                h, m, s = (tc.split(':') + ['0','0','0'])[:3]
                used = int(h)*3600 + int(m)*60 + int(float(s))
            except (ValueError, AttributeError):
                continue
            if allocated == 0:
                continue
            if user not in eff:
                eff[user] = {'allocated': 0, 'used': 0, 'jobs': 0}
            eff[user]['allocated'] += allocated
            eff[user]['used'] += used
            eff[user]['jobs'] += 1
        data['efficiency'] = [
            {'user': u, 'efficiency_pct': round(v['used']/v['allocated']*100, 1),
             'jobs': v['jobs'], 'cpu_hours_alloc': round(v['allocated']/3600,1)}
            for u, v in sorted(eff.items(), key=lambda x: x[1]['allocated'], reverse=True)
            if v['allocated'] > 0
        ]
    except Exception:
        data['efficiency'] = []

    _puhti_cache = data
    _puhti_cache_ts = time.time()
    return data

# ── DB helper ─────────────────────────────────────────────────────────────────

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    # Migrate cpus/memory_gb columns
    cols = [r[1] for r in conn.execute('PRAGMA table_info(runs)').fetchall()]
    if 'cpus' not in cols:
        conn.execute('ALTER TABLE runs ADD COLUMN cpus INTEGER DEFAULT 0')
    if 'memory_gb' not in cols:
        conn.execute('ALTER TABLE runs ADD COLUMN memory_gb INTEGER DEFAULT 0')
    conn.commit()
    return conn

# ── Admin API endpoints ───────────────────────────────────────────────────────

@router.get('/jobs')
def admin_jobs(
    request: Request,
    username: str = '',
    status: str = '',
    admin_session: str | None = Cookie(default=None),
):
    _require_auth(admin_session)
    query = 'SELECT job_id, slurm_id, status, partition, username, email, cpus, memory_gb, created FROM runs WHERE 1=1'
    params: list = []
    if username:
        query += ' AND username=?'; params.append(username)
    if status:
        query += ' AND status=?'; params.append(status)
    query += ' ORDER BY created DESC LIMIT 200'
    with contextlib.closing(_db()) as db:
        rows = db.execute(query, params).fetchall()
    return {'jobs': [dict(r) for r in rows]}


@router.get('/stats')
def admin_stats(admin_session: str | None = Cookie(default=None)):
    _require_auth(admin_session)
    with contextlib.closing(_db()) as db:
        rows = db.execute('''
            SELECT username,
                   COUNT(*) as total,
                   SUM(CASE WHEN status="done"      THEN 1 ELSE 0 END) as done,
                   SUM(CASE WHEN status="failed"    THEN 1 ELSE 0 END) as failed,
                   SUM(CASE WHEN status IN ("queued","running") THEN 1 ELSE 0 END) as active,
                   SUM(CASE WHEN status="running" THEN 1 ELSE 0 END) as running,
                   SUM(CASE WHEN status="cancelled" THEN 1 ELSE 0 END) as cancelled,
                   MAX(created) as last_active
            FROM runs GROUP BY username ORDER BY last_active DESC
        ''').fetchall()
    return {'stats': [dict(r) for r in rows]}


@router.get('/container-requests')
def admin_container_requests(admin_session: str | None = Cookie(default=None)):
    _require_auth(admin_session)
    with contextlib.closing(_db()) as db:
        rows = db.execute(
            'SELECT id, username, container, pr_url, pr_number, status, created '
            'FROM container_requests ORDER BY created DESC LIMIT 100'
        ).fetchall()
    return {'requests': [dict(r) for r in rows]}


@router.get('/puhti')
def admin_puhti(admin_session: str | None = Cookie(default=None)):
    _require_auth(admin_session)
    return _puhti_data()


@router.get('/history')
def admin_history(days: int = 30, username: str = '', admin_session: str | None = Cookie(default=None)):
    """Job counts per day for the last N days, grouped by status. Optional username filter."""
    _require_auth(admin_session)
    with contextlib.closing(_db()) as db:
        if username:
            rows = db.execute('''
                SELECT date(created) as day,
                       SUM(CASE WHEN status="done"      THEN 1 ELSE 0 END) as done,
                       SUM(CASE WHEN status="failed"    THEN 1 ELSE 0 END) as failed,
                       SUM(CASE WHEN status="cancelled" THEN 1 ELSE 0 END) as cancelled,
                       COUNT(*) as total
                FROM runs
                WHERE created >= date("now", ?) AND username=?
                GROUP BY day ORDER BY day ASC
            ''', (f'-{days} days', username)).fetchall()
        else:
            rows = db.execute('''
                SELECT date(created) as day,
                       SUM(CASE WHEN status="done"      THEN 1 ELSE 0 END) as done,
                       SUM(CASE WHEN status="failed"    THEN 1 ELSE 0 END) as failed,
                       SUM(CASE WHEN status="cancelled" THEN 1 ELSE 0 END) as cancelled,
                       COUNT(*) as total
                FROM runs
                WHERE created >= date("now", ?)
                GROUP BY day ORDER BY day ASC
            ''', (f'-{days} days',)).fetchall()
    return {'history': [dict(r) for r in rows]}


@router.get('/history-by-user')
def admin_history_by_user(days: int = 30, admin_session: str | None = Cookie(default=None)):
    """Per-user job totals for the last N days."""
    _require_auth(admin_session)
    with contextlib.closing(_db()) as db:
        rows = db.execute('''
            SELECT username,
                   COUNT(*) as total,
                   SUM(CASE WHEN status="done"      THEN 1 ELSE 0 END) as done,
                   SUM(CASE WHEN status="failed"    THEN 1 ELSE 0 END) as failed,
                   SUM(CASE WHEN status="cancelled" THEN 1 ELSE 0 END) as cancelled,
                   MAX(created) as last_active
            FROM runs
            WHERE created >= date("now", ?)
            GROUP BY username ORDER BY total DESC
        ''', (f'-{days} days',)).fetchall()
    return {'users': [dict(r) for r in rows]}


@router.get('/refresh-puhti')
def admin_refresh_puhti(admin_session: str | None = Cookie(default=None)):
    """Force a fresh SSH pull, ignoring cache."""
    _require_auth(admin_session)
    global _puhti_cache_ts
    _puhti_cache_ts = 0
    return _puhti_data()

# ── Admin HTML page ───────────────────────────────────────────────────────────

@router.get('/login-page', response_class=HTMLResponse)
def login_page():
    return HTMLResponse(_LOGIN_HTML)


@router.get('', response_class=HTMLResponse)
@router.get('/', response_class=HTMLResponse)
def admin_page(admin_session: str | None = Cookie(default=None)):
    user = _verify_session(admin_session) if admin_session else None
    if not user:
        return RedirectResponse(f'{PUBLIC_URL}/admin/login')
    return HTMLResponse(_ADMIN_HTML.replace('__USER__', user).replace('__BASE__', PUBLIC_URL))


_LOGIN_HTML = '''<!doctype html>
<html><head><title>Admin Login</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0f172a;color:#f1f5f9;}
  .card{background:#1e293b;border-radius:12px;padding:40px;text-align:center;max-width:340px;width:100%;}
  h1{font-size:20px;margin:0 0 8px;}
  p{color:#94a3b8;font-size:14px;margin:0 0 28px;}
  a{display:inline-flex;align-items:center;gap:10px;background:#238636;color:white;
    text-decoration:none;padding:12px 24px;border-radius:8px;font-size:14px;font-weight:600;}
  a:hover{background:#2ea043;}
  svg{width:20px;height:20px;fill:white;}
</style></head><body>
<div class="card">
  <h1>⚡ Puhti Admin</h1>
  <p>Sign in with your DIWA GitHub account to continue.</p>
  <a href="__BASE__/admin/login">
    <svg viewBox="0 0 16 16"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38
    0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52
    -.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2
    -3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82
    .64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08
    2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01
    1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
    Sign in with GitHub
  </a>
</div>
</body></html>'''.replace('__BASE__', PUBLIC_URL)


_ADMIN_HTML = r'''<!doctype html>
<html><head><title>Puhti Admin</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0d1117;--bg2:#161b22;--bg3:#1c2128;--border:#30363d;--text:#e6edf3;--text2:#8b949e;--blue:#58a6ff;--green:#3fb950;--red:#f85149;--orange:#d29922;--purple:#bc8cff;--cyan:#39d353}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--text);display:flex;min-height:100vh;font-size:13px}
/* Sidebar */
.sidebar{width:220px;background:var(--bg2);border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0;position:fixed;top:0;left:0;height:100vh;z-index:20}
.sidebar-logo{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}
.sidebar-logo span{font-size:14px;font-weight:700;color:var(--text)}
.sidebar-logo small{font-size:10px;color:var(--text2);display:block;margin-top:1px}
.nav-section{padding:8px 0}
.nav-label{padding:6px 20px 4px;font-size:10px;color:var(--text2);text-transform:uppercase;letter-spacing:.8px;font-weight:600}
.nav-item{display:flex;align-items:center;gap:10px;padding:8px 20px;cursor:pointer;color:var(--text2);font-size:12px;border-left:2px solid transparent;transition:all .15s}
.nav-item:hover{color:var(--text);background:var(--bg3)}
.nav-item.active{color:var(--blue);background:rgba(88,166,255,.08);border-left-color:var(--blue)}
.nav-item svg{width:15px;height:15px;flex-shrink:0;fill:currentColor}
.sidebar-footer{margin-top:auto;padding:12px 20px;border-top:1px solid var(--border);font-size:11px;color:var(--text2)}
.sidebar-footer a{color:var(--text2);text-decoration:none}
.sidebar-footer a:hover{color:var(--text)}
/* Main */
.main{margin-left:220px;flex:1;display:flex;flex-direction:column;min-height:100vh}
.topbar{background:var(--bg2);border-bottom:1px solid var(--border);padding:10px 24px;display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:10}
.topbar h2{font-size:14px;font-weight:600;flex:1}
.topbar-meta{font-size:11px;color:var(--text2);display:flex;align-items:center;gap:12px}
.live-dot{width:7px;height:7px;border-radius:50%;background:var(--green);display:inline-block;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.content{padding:20px 24px;flex:1}
.page{display:none}
.page.active{display:block}
/* Cards */
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:14px 16px}
.card-val{font-size:28px;font-weight:700;line-height:1;margin-bottom:4px}
.card-lbl{font-size:10px;color:var(--text2);text-transform:uppercase;letter-spacing:.5px}
.card-sub{font-size:10px;color:var(--text2);margin-top:3px}
/* Charts */
.chart-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}
.chart-box{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:16px}
.chart-title{font-size:12px;font-weight:600;color:var(--text);margin-bottom:12px;display:flex;justify-content:space-between;align-items:center}
.chart-title small{font-size:10px;color:var(--text2);font-weight:400}
canvas{width:100%;display:block}
/* Tables */
.tbl-box{background:var(--bg2);border:1px solid var(--border);border-radius:8px;margin-bottom:16px;overflow:hidden}
.tbl-header{padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.tbl-header h3{font-size:13px;font-weight:600;flex:1}
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:8px 12px;color:var(--text2);font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border);background:var(--bg3);white-space:nowrap}
td{padding:7px 12px;border-bottom:1px solid rgba(48,54,61,.5);white-space:nowrap;color:var(--text)}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(88,166,255,.04)}
/* Badges */
.badge{padding:2px 8px;border-radius:12px;font-size:10px;font-weight:700;display:inline-block;text-transform:uppercase;letter-spacing:.3px}
.done{background:rgba(63,185,80,.15);color:var(--green)}
.failed{background:rgba(248,81,73,.15);color:var(--red)}
.queued,.pending{background:rgba(210,153,34,.15);color:var(--orange)}
.running{background:rgba(88,166,255,.15);color:var(--blue)}
.cancelled,.closed{background:rgba(139,148,158,.1);color:var(--text2)}
.merged,.up{background:rgba(63,185,80,.15);color:var(--green)}
.down{background:rgba(248,81,73,.15);color:var(--red)}
/* Form controls */
.filters{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
input,select{background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:5px 10px;border-radius:6px;font-size:12px;height:30px;font-family:inherit}
input:focus,select:focus{outline:none;border-color:var(--blue)}
.btn{background:var(--blue);color:#0d1117;border:none;padding:5px 12px;border-radius:6px;font-size:11px;cursor:pointer;height:30px;font-weight:600}
.btn:hover{background:#79c0ff}
.btn.sm{height:24px;padding:3px 8px;font-size:10px}
.btn.ghost{background:transparent;color:var(--text2);border:1px solid var(--border)}
.btn.ghost:hover{color:var(--text);border-color:var(--text2)}
/* Util */
.chip{background:var(--bg3);border:1px solid var(--border);color:var(--text2);padding:1px 8px;border-radius:20px;font-size:10px;font-weight:600}
.bar-wrap{display:flex;align-items:center;gap:6px}
.bar{height:5px;border-radius:3px;background:var(--bg3);flex:1;overflow:hidden;min-width:60px}
.bar-fill{height:100%;border-radius:3px;background:var(--blue);transition:width .3s}
.bar-fill.warn{background:var(--orange)}
.bar-fill.danger{background:var(--red)}
.pct-label{font-size:10px;color:var(--text2);min-width:32px;text-align:right}
pre.billing{background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:12px;font-size:11px;color:var(--text2);white-space:pre-wrap;font-family:monospace}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.search-input{background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:5px 10px 5px 30px;border-radius:6px;font-size:12px;height:30px;width:220px;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' fill='%238b949e' viewBox='0 0 16 16'%3E%3Cpath d='M11.742 10.344a6.5 6.5 0 1 0-1.397 1.398h-.001c.03.04.062.078.098.115l3.85 3.85a1 1 0 0 0 1.415-1.414l-3.85-3.85a1.007 1.007 0 0 0-.115-.099zm-5.242 1.656a5.5 5.5 0 1 1 0-11 5.5 5.5 0 0 1 0 11z'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:8px center;background-size:13px}
.refresh-ts{font-size:10px;color:var(--text2)}
@media(max-width:768px){.sidebar{width:52px}.sidebar-logo small,.sidebar-logo span,.nav-label,.nav-item span{display:none}.main{margin-left:52px}.chart-grid,.two-col{grid-template-columns:1fr}.cards{grid-template-columns:repeat(2,1fr)}}
</style>
</head><body>

<!-- SIDEBAR -->
<aside class="sidebar">
  <div class="sidebar-logo">
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none"><rect width="24" height="24" rx="6" fill="#58a6ff"/><path d="M6 8h12M6 12h12M6 16h8" stroke="#0d1117" stroke-width="2" stroke-linecap="round"/></svg>
    <div><span>Puhti Admin</span><small>project_2014823</small></div>
  </div>
  <div class="nav-section">
    <div class="nav-label">Monitor</div>
    <div class="nav-item active" onclick="nav('dashboard')">
      <svg viewBox="0 0 16 16"><path d="M1 2a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1H2a1 1 0 0 1-1-1V2zm5 0a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V2zm5 0a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1h-2a1 1 0 0 1-1-1V2zM1 7a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1H2a1 1 0 0 1-1-1V7zm5 0a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V7zm5 0a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1h-2a1 1 0 0 1-1-1V7zM1 12a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1H2a1 1 0 0 1-1-1v-2zm5 0a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1v-2z"/></svg>
      <span>Dashboard</span>
    </div>
    <div class="nav-item" onclick="nav('queue')">
      <svg viewBox="0 0 16 16"><path d="M2 2a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v13.5a.5.5 0 0 1-.777.416L8 13.101l-5.223 2.815A.5.5 0 0 1 2 15.5V2z"/></svg>
      <span>Live Queue</span>
    </div>
    <div class="nav-item" onclick="nav('partitions')">
      <svg viewBox="0 0 16 16"><path d="M1 2.5A1.5 1.5 0 0 1 2.5 1h3A1.5 1.5 0 0 1 7 2.5v3A1.5 1.5 0 0 1 5.5 7h-3A1.5 1.5 0 0 1 1 5.5v-3zm8 0A1.5 1.5 0 0 1 10.5 1h3A1.5 1.5 0 0 1 15 2.5v3A1.5 1.5 0 0 1 13.5 7h-3A1.5 1.5 0 0 1 9 5.5v-3zm-8 8A1.5 1.5 0 0 1 2.5 9h3A1.5 1.5 0 0 1 7 10.5v3A1.5 1.5 0 0 1 5.5 15h-3A1.5 1.5 0 0 1 1 13.5v-3zm8 0A1.5 1.5 0 0 1 10.5 9h3a1.5 1.5 0 0 1 1.5 1.5v3a1.5 1.5 0 0 1-1.5 1.5h-3A1.5 1.5 0 0 1 9 13.5v-3z"/></svg>
      <span>Partitions</span>
    </div>
  </div>
  <div class="nav-section">
    <div class="nav-label">Jobs</div>
    <div class="nav-item" onclick="nav('jobs')">
      <svg viewBox="0 0 16 16"><path d="M0 2a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H4.414a1 1 0 0 0-.707.293L.854 15.146A.5.5 0 0 1 0 14.793V2z"/></svg>
      <span>All Jobs</span>
    </div>
    <div class="nav-item" onclick="nav('history')">
      <svg viewBox="0 0 16 16"><path d="M8 3.5a.5.5 0 0 0-1 0V9a.5.5 0 0 0 .252.434l3.5 2a.5.5 0 0 0 .496-.868L8 8.71V3.5z"/><path d="M8 16A8 8 0 1 0 8 0a8 8 0 0 0 0 16zm7-8A7 7 0 1 1 1 8a7 7 0 0 1 14 0z"/></svg>
      <span>History</span>
    </div>
  </div>
  <div class="nav-section">
    <div class="nav-label">Admin</div>
    <div class="nav-item" onclick="nav('users')">
      <svg viewBox="0 0 16 16"><path d="M7 14s-1 0-1-1 1-4 5-4 5 3 5 4-1 1-1 1H7zm4-6a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"/><path fill-rule="evenodd" d="M5.216 14A2.238 2.238 0 0 1 5 13c0-1.355.68-2.75 1.936-3.72A6.325 6.325 0 0 0 5 9c-4 0-5 3-5 4s1 1 1 1h4.216z"/><path d="M4.5 8a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z"/></svg>
      <span>Users</span>
    </div>
    <div class="nav-item" onclick="nav('containers')">
      <svg viewBox="0 0 16 16"><path d="M8.235 1.559a.5.5 0 0 0-.47 0l-7.5 4a.5.5 0 0 0 0 .882L3.188 8 .264 9.559a.5.5 0 0 0 0 .882l7.5 4a.5.5 0 0 0 .47 0l7.5-4a.5.5 0 0 0 0-.882L12.813 8l2.922-1.559a.5.5 0 0 0 0-.882l-7.5-4z"/></svg>
      <span>Containers</span>
    </div>
    <div class="nav-item" onclick="nav('puhti')">
      <svg viewBox="0 0 16 16"><path d="M11 1a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1h6zM5 0a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2V2a2 2 0 0 0-2-2H5z"/><path d="M8 14a1 1 0 1 0 0-2 1 1 0 0 0 0 2z"/></svg>
      <span>Puhti System</span>
    </div>
  </div>
  <div class="sidebar-footer">
    <div style="margin-bottom:6px">Signed in as <strong>__USER__</strong></div>
    <a href="__BASE__/admin/logout">Sign out</a>
  </div>
</aside>

<!-- MAIN -->
<div class="main">
  <div class="topbar">
    <h2 id="page-title">Dashboard</h2>
    <div class="topbar-meta">
      <span><span class="live-dot"></span> Live</span>
      <span class="refresh-ts" id="refresh-ts">—</span>
      <button class="btn ghost sm" onclick="refreshAll()">↻ Refresh</button>
    </div>
  </div>

  <div class="content">

  <!-- DASHBOARD -->
  <div class="page active" id="page-dashboard">
    <div class="cards" id="dash-cards">
      <div class="card"><div class="card-val" id="d-total" style="color:var(--blue)">—</div><div class="card-lbl">Total Jobs</div></div>
      <div class="card"><div class="card-val" id="d-running" style="color:var(--blue)">—</div><div class="card-lbl">Running</div></div>
      <div class="card"><div class="card-val" id="d-queued" style="color:var(--orange)">—</div><div class="card-lbl">Queued</div></div>
      <div class="card"><div class="card-val" id="d-done" style="color:var(--green)">—</div><div class="card-lbl">Completed</div></div>
      <div class="card"><div class="card-val" id="d-failed" style="color:var(--red)">—</div><div class="card-lbl">Failed</div></div>
      <div class="card"><div class="card-val" id="d-users" style="color:var(--purple)">—</div><div class="card-lbl">Users</div></div>
      <div class="card"><div class="card-val" id="d-scratch" style="color:var(--cyan)">—</div><div class="card-lbl">Scratch Used</div><div class="card-sub">Puhti /scratch</div></div>
      <div class="card"><div class="card-val" id="d-cpu" style="color:var(--orange)">—</div><div class="card-lbl">CPU Hours (month)</div></div>
    </div>
    <div class="chart-grid">
      <div class="chart-box">
        <div class="chart-title">Job History — Last 30 Days <small>by status</small></div>
        <canvas id="chart-history" height="160"></canvas>
      </div>
      <div class="chart-box">
        <div class="chart-title">Partition Utilization <small>CPUs alloc/total</small></div>
        <canvas id="chart-partitions" height="160"></canvas>
      </div>
    </div>
    <div class="two-col" style="margin-bottom:16px">
      <div class="tbl-box">
        <div class="tbl-header"><h3>Active Jobs</h3><span class="chip" id="d-active-count">0</span></div>
        <div class="tbl-wrap">
        <table>
          <thead><tr><th>User</th><th>Slurm Job</th><th>Partition</th><th>State</th><th>Time Limit</th></tr></thead>
          <tbody id="d-queue-body"></tbody>
        </table>
        </div>
      </div>
      <div class="tbl-box">
        <div class="tbl-header"><h3>Top Users — All Time</h3></div>
        <div class="tbl-wrap">
        <table>
          <thead><tr><th>User</th><th>Total Jobs</th><th>Done</th><th>Failed</th><th>Last Active</th></tr></thead>
          <tbody id="d-top-users-body"></tbody>
        </table>
        </div>
      </div>
    </div>
  </div>

  <!-- LIVE QUEUE -->
  <div class="page" id="page-queue">
    <div class="tbl-box">
      <div class="tbl-header">
        <h3>Live Queue — project_2014823</h3>
        <span class="chip" id="q-count">0</span>
      </div>
      <div class="tbl-wrap">
      <table>
        <thead><tr><th>User</th><th>Slurm Job</th><th>Partition</th><th>State</th><th>Time Limit</th><th>Submit Time</th></tr></thead>
        <tbody id="queue-body"></tbody>
      </table>
      </div>
    </div>
  </div>

  <!-- PARTITIONS -->
  <div class="page" id="page-partitions">
    <div class="tbl-box">
      <div class="tbl-header"><h3>Puhti Partitions</h3></div>
      <div class="tbl-wrap">
      <table>
        <thead><tr><th>Partition</th><th>Status</th><th>Nodes</th><th>CPUs Alloc</th><th>CPUs Idle</th><th>CPUs Total</th><th>Utilization</th></tr></thead>
        <tbody id="partition-body"></tbody>
      </table>
      </div>
    </div>
  </div>

  <!-- ALL JOBS -->
  <div class="page" id="page-jobs">
    <div class="tbl-box">
      <div class="tbl-header">
        <h3>All Jobs</h3>
        <span class="chip" id="jobs-count">0</span>
        <div class="filters" style="margin-left:auto">
          <input class="search-input" id="filter-user" placeholder="Filter by username…" oninput="loadJobs()">
          <select id="filter-status" onchange="loadJobs()">
            <option value="">All statuses</option>
            <option>queued</option><option>running</option>
            <option>done</option><option>failed</option><option>cancelled</option>
          </select>
        </div>
      </div>
      <div class="tbl-wrap">
      <table>
        <thead><tr><th>Job ID</th><th>Slurm</th><th>User</th><th>Status</th><th>Partition</th><th>CPUs</th><th>RAM</th><th>Submitted</th></tr></thead>
        <tbody id="jobs-body"></tbody>
      </table>
      </div>
    </div>
  </div>

  <!-- HISTORY -->
  <div class="page" id="page-history">
    <div class="tbl-header" style="background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:12px 16px;margin-bottom:16px;gap:12px">
      <h3 style="font-size:13px;font-weight:600">Job History</h3>
      <div class="filters" style="margin-left:auto">
        <select id="hist-user-filter" onchange="loadHistory()" style="width:180px">
          <option value="">All users</option>
        </select>
        <select id="hist-days-filter" onchange="loadHistory()">
          <option value="30">Last 30 days</option>
          <option value="60">Last 60 days</option>
          <option value="90">Last 90 days</option>
        </select>
      </div>
    </div>
    <div class="cards" style="margin-bottom:20px">
      <div class="card"><div class="card-val" id="h-total" style="color:var(--blue)">—</div><div class="card-lbl">Total Jobs</div></div>
      <div class="card"><div class="card-val" id="h-done" style="color:var(--green)">—</div><div class="card-lbl">Completed</div></div>
      <div class="card"><div class="card-val" id="h-failed" style="color:var(--red)">—</div><div class="card-lbl">Failed</div></div>
      <div class="card"><div class="card-val" id="h-cancelled" style="color:var(--text2)">—</div><div class="card-lbl">Cancelled</div></div>
      <div class="card"><div class="card-val" id="h-rate" style="color:var(--cyan)">—</div><div class="card-lbl">Success Rate</div></div>
    </div>
    <div class="chart-box" style="margin-bottom:16px">
      <div class="chart-title">Daily Job Activity <small id="hist-chart-label">last 30 days — all users</small></div>
      <canvas id="chart-history2" height="180"></canvas>
    </div>
    <div class="tbl-box">
      <div class="tbl-header"><h3>Daily Breakdown</h3></div>
      <div class="tbl-wrap">
      <table>
        <thead><tr><th>Date</th><th>Total</th><th>Done</th><th>Failed</th><th>Cancelled</th><th>Success Rate</th></tr></thead>
        <tbody id="history-body"></tbody>
      </table>
      </div>
    </div>
    <div class="tbl-box" style="margin-top:16px">
      <div class="tbl-header">
        <h3>Breakdown by User</h3>
        <span class="chip" id="hbu-count">0</span>
        <small style="color:var(--text2);font-size:10px;margin-left:8px" id="hbu-period"></small>
      </div>
      <div class="tbl-wrap">
      <table>
        <thead><tr><th>User</th><th>Total Jobs</th><th>Done</th><th>Failed</th><th>Cancelled</th><th>Success Rate</th><th>Last Active</th><th></th></tr></thead>
        <tbody id="hbu-body"></tbody>
      </table>
      </div>
    </div>
  </div>

  <!-- USERS -->
  <div class="page" id="page-users">
    <div class="tbl-box">
      <div class="tbl-header">
        <h3>Users</h3>
        <span class="chip" id="u-count">0</span>
        <input class="search-input" id="user-search" placeholder="Search users…" oninput="filterUsers()" style="margin-left:auto">
      </div>
      <div class="tbl-wrap">
      <table>
        <thead><tr><th>User</th><th>Total Jobs</th><th>Active</th><th>Done</th><th>Failed</th><th>Cancelled</th><th>Last Active</th><th></th></tr></thead>
        <tbody id="users-body"></tbody>
      </table>
      </div>
    </div>
  </div>

  <!-- CONTAINERS -->
  <div class="page" id="page-containers">
    <div class="tbl-box">
      <div class="tbl-header">
        <h3>Container Requests</h3>
        <span class="chip" id="cr-count">0</span>
        <span id="cr-pending" style="margin-left:8px;font-size:11px;color:var(--orange)"></span>
      </div>
      <div class="tbl-wrap">
      <table>
        <thead><tr><th>User</th><th>Container</th><th>Status</th><th>PR</th><th>Requested</th></tr></thead>
        <tbody id="containers-body"></tbody>
      </table>
      </div>
    </div>
  </div>

  <!-- PUHTI SYSTEM -->
  <div class="page" id="page-puhti">
    <div class="two-col" style="margin-bottom:16px">
      <div class="chart-box">
        <div class="chart-title">Billing Units — project_2014823</div>
        <pre class="billing" id="billing-text">Loading…</pre>
      </div>
      <div class="tbl-box">
        <div class="tbl-header"><h3>Pending Reasons</h3><span class="chip" id="pr-count">0</span></div>
        <div class="tbl-wrap">
        <table>
          <thead><tr><th>Reason</th><th>Jobs</th></tr></thead>
          <tbody id="pending-body"></tbody>
        </table>
        </div>
      </div>
    </div>
    <div class="two-col" style="margin-bottom:16px">
      <div class="tbl-box">
        <div class="tbl-header"><h3>Scratch Disk by User</h3></div>
        <div class="tbl-wrap">
        <table>
          <thead><tr><th>User</th><th>Used</th></tr></thead>
          <tbody id="disk-body"></tbody>
        </table>
        </div>
      </div>
      <div class="tbl-box">
        <div class="tbl-header"><h3>CPU Hours This Month</h3></div>
        <div class="tbl-wrap">
        <table>
          <thead><tr><th>User</th><th>CPU Hours</th><th>Jobs</th></tr></thead>
          <tbody id="usage-body"></tbody>
        </table>
        </div>
      </div>
    </div>
    <div class="tbl-box" style="margin-bottom:16px">
      <div class="tbl-header"><h3>CPU Efficiency — Last 30 Days</h3><small style="color:var(--text2);font-size:10px;margin-left:8px">actual CPU used vs allocated</small></div>
      <div class="tbl-wrap">
      <table>
        <thead><tr><th>User</th><th>Jobs</th><th>CPU Hours Alloc</th><th>Efficiency</th></tr></thead>
        <tbody id="eff-body"></tbody>
      </table>
      </div>
    </div>
    <div class="tbl-box" style="margin-bottom:16px">
      <div class="tbl-header">
        <h3>GPU Nodes</h3>
        <span class="chip" id="gpu-count">0</span>
        <div style="margin-left:auto;display:flex;align-items:center;gap:8px">
          <button class="btn sm ghost" id="gpu-prev" onclick="gpuPage(-1)">◀</button>
          <span id="gpu-page-label" style="font-size:11px;color:var(--text2)">—</span>
          <button class="btn sm ghost" id="gpu-next" onclick="gpuPage(1)">▶</button>
        </div>
      </div>
      <div class="tbl-wrap">
      <table>
        <thead><tr><th>Node</th><th>State</th><th>GPUs</th><th>CPUs Alloc</th><th>CPUs Total</th></tr></thead>
        <tbody id="gpu-body"></tbody>
      </table>
      </div>
    </div>
    <div class="tbl-box">
      <div class="tbl-header">
        <h3>Node Health</h3>
        <span class="chip" id="node-count">0</span>
        <div class="filters" style="margin-left:auto">
          <select id="node-state-filter" onchange="filterNodes()">
            <option value="">All states</option>
            <option>idle</option><option>allocated</option><option>mixed</option>
            <option>drain</option><option>down</option>
          </select>
        </div>
      </div>
      <div class="tbl-wrap">
      <table>
        <thead><tr><th>Node</th><th>State</th><th>CPUs Alloc</th><th>CPUs Idle</th><th>CPUs Total</th><th>Mem Total</th><th>Mem Free</th><th>Utilization</th></tr></thead>
        <tbody id="node-body"></tbody>
      </table>
      </div>
    </div>
  </div>

  </div><!-- /content -->
</div><!-- /main -->

<script>
const BASE = '__BASE__';
const PAGES = ['dashboard','queue','partitions','jobs','history','users','containers','puhti'];
let _stats=[], _puhti={}, _history=[];

function nav(name) {
  document.querySelectorAll('.nav-item').forEach(el=>el.classList.remove('active'));
  document.querySelectorAll('.page').forEach(el=>el.classList.remove('active'));
  const idx = PAGES.indexOf(name);
  document.querySelectorAll('.nav-item')[idx].classList.add('active');
  document.getElementById('page-'+name).classList.add('active');
  const titles={dashboard:'Dashboard',queue:'Live Queue',partitions:'Partitions',jobs:'All Jobs',history:'Job History',users:'Users',containers:'Container Requests',puhti:'Puhti System'};
  document.getElementById('page-title').textContent = titles[name]||name;
  if(name==='dashboard'){loadStats();loadPuhti();loadHistory();}
  if(name==='queue'||name==='partitions')loadPuhti();
  if(name==='jobs')loadJobs();
  if(name==='history')loadHistory();
  if(name==='users')loadStats();
  if(name==='containers')loadContainerRequests();
  if(name==='puhti')loadPuhti();
}

async function api(path){
  const r=await fetch(BASE+path,{credentials:'include'});
  if(r.status===302||r.status===401){location.href=BASE+'/admin/login';return null;}
  if(!r.ok)throw new Error(await r.text());
  return r.json();
}

const B=s=>`<span class="badge ${s}">${s}</span>`;
const fmt=dt=>dt?new Date(dt.replace(' ','T')+'Z').toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'—';
const fmtDate=dt=>dt?new Date(dt+'T00:00:00Z').toLocaleDateString(undefined,{month:'short',day:'numeric'}):'—';
const pct=(a,t)=>t>0?Math.round(a/t*100):0;

function barHTML(used,total){
  const p=pct(used,total);
  const cls=p>90?'danger':p>70?'warn':'';
  return `<div class="bar-wrap"><div class="bar"><div class="bar-fill ${cls}" style="width:${p}%"></div></div><span class="pct-label">${p}%</span></div>`;
}

// ── Charts ────────────────────────────────────────────────────────────────────
function drawHistoryChart(canvasId, data) {
  const canvas = document.getElementById(canvasId);
  if(!canvas) return;
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio||1;
  const W = canvas.offsetWidth||600, H = parseInt(canvas.getAttribute('height'))||160;
  canvas.width = W*dpr; canvas.height = H*dpr;
  ctx.scale(dpr,dpr);
  ctx.clearRect(0,0,W,H);

  if(!data.length){ctx.fillStyle='#8b949e';ctx.font='12px sans-serif';ctx.textAlign='center';ctx.fillText('No data yet',W/2,H/2);return;}

  const pad={t:10,r:10,b:30,l:36};
  const cW=W-pad.l-pad.r, cH=H-pad.t-pad.b;
  const maxVal=Math.max(...data.map(d=>d.total),1);
  const barW=Math.max(2,cW/data.length-2);

  // Grid lines
  ctx.strokeStyle='rgba(48,54,61,0.8)';ctx.lineWidth=1;
  for(let i=0;i<=4;i++){
    const y=pad.t+cH*(1-i/4);
    ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(pad.l+cW,y);ctx.stroke();
    ctx.fillStyle='#8b949e';ctx.font='9px sans-serif';ctx.textAlign='right';
    ctx.fillText(Math.round(maxVal*i/4),pad.l-4,y+3);
  }

  // Bars
  data.forEach((d,i)=>{
    const x=pad.l+i*(cW/data.length);
    const drawBar=(val,color,yOff)=>{
      const h=Math.max(1,(val/maxVal)*cH);
      ctx.fillStyle=color;
      ctx.fillRect(x+1,pad.t+cH-yOff-h,barW,h);
      return h;
    };
    let off=0;
    off+=drawBar(d.failed,'rgba(248,81,73,0.85)',off);
    off+=drawBar(d.cancelled,'rgba(139,148,158,0.5)',off);
    off+=drawBar(d.done,'rgba(63,185,80,0.85)',off);
  });

  // X labels — show every Nth
  const step=Math.max(1,Math.floor(data.length/8));
  ctx.fillStyle='#8b949e';ctx.font='9px sans-serif';ctx.textAlign='center';
  data.forEach((d,i)=>{
    if(i%step===0){
      const x=pad.l+i*(cW/data.length)+barW/2;
      const parts=d.day.split('-');
      ctx.fillText(`${parts[1]}/${parts[2]}`,x,H-pad.b+12);
    }
  });

  // Legend
  const leg=[['Done','rgba(63,185,80,0.85)'],['Failed','rgba(248,81,73,0.85)'],['Cancelled','rgba(139,148,158,0.5)']];
  leg.forEach(([lbl,col],i)=>{
    const lx=W-200+i*65;
    ctx.fillStyle=col;ctx.fillRect(lx,6,10,8);
    ctx.fillStyle='#8b949e';ctx.font='9px sans-serif';ctx.textAlign='left';
    ctx.fillText(lbl,lx+13,13);
  });
}

function drawPartitionChart(canvasId, partitions) {
  const canvas = document.getElementById(canvasId);
  if(!canvas||!partitions.length) return;
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio||1;
  const W = canvas.offsetWidth||600, H = parseInt(canvas.getAttribute('height'))||160;
  canvas.width = W*dpr; canvas.height = H*dpr;
  ctx.scale(dpr,dpr);
  ctx.clearRect(0,0,W,H);

  const relevant = partitions.filter(p=>['small','large','gpu','gpumedium','longrun'].includes(p.partition));
  if(!relevant.length) return;
  const pad={t:10,r:10,b:30,l:90};
  const cW=W-pad.l-pad.r, cH=H-pad.t-pad.b;
  const rowH=cH/relevant.length;

  relevant.forEach((p,i)=>{
    const y=pad.t+i*rowH+rowH*0.2;
    const bH=rowH*0.5;
    const alloc=parseInt(p.cpus_alloc)||0;
    const total=parseInt(p.cpus_total)||1;
    const p_=pct(alloc,total);
    const color=p_>90?'#f85149':p_>70?'#d29922':'#58a6ff';

    ctx.fillStyle='#8b949e';ctx.font='11px sans-serif';ctx.textAlign='right';
    ctx.fillText(p.partition,pad.l-8,y+bH*0.7);

    ctx.fillStyle='#1c2128';
    ctx.fillRect(pad.l,y,cW,bH);
    ctx.fillStyle=color;
    ctx.fillRect(pad.l,y,cW*(p_/100),bH);

    ctx.fillStyle='#e6edf3';ctx.font='10px sans-serif';ctx.textAlign='left';
    ctx.fillText(`${p_}% (${alloc}/${total})`,pad.l+6,y+bH*0.7);

    // Grid line
    ctx.strokeStyle='rgba(48,54,61,0.5)';ctx.lineWidth=1;
    ctx.beginPath();ctx.moveTo(pad.l,y+bH+rowH*0.2);ctx.lineTo(pad.l+cW,y+bH+rowH*0.2);ctx.stroke();
  });
}

// ── Data loaders ──────────────────────────────────────────────────────────────
async function loadStats(){
  const data=await api('/admin/stats');
  if(!data)return;
  _stats=data.stats;
  const total=_stats.reduce((s,r)=>s+r.total,0);
  const active=_stats.reduce((s,r)=>s+r.active,0);
  const done=_stats.reduce((s,r)=>s+r.done,0);
  const failed=_stats.reduce((s,r)=>s+r.failed,0);
  const running=_stats.reduce((s,r)=>s+(r.running||0),0);
  const queued=active-running;

  document.getElementById('d-total').textContent=total;
  document.getElementById('d-running').textContent=running||active;
  document.getElementById('d-queued').textContent=queued>0?queued:0;
  document.getElementById('d-done').textContent=done;
  document.getElementById('d-failed').textContent=failed;
  document.getElementById('d-users').textContent=_stats.length;
  document.getElementById('u-count').textContent=_stats.length;
  renderUsers(_stats);

  // Top-5 dashboard table
  const top5=[..._stats].sort((a,b)=>b.total-a.total).slice(0,5);
  document.getElementById('d-top-users-body').innerHTML=top5.map(r=>`<tr>
    <td><a href="#" onclick="jumpToJobs('${r.username||''}');return false" style="color:var(--blue);text-decoration:none">${r.username||'—'}</a></td>
    <td>${r.total}</td>
    <td><span style="color:var(--green)">${r.done}</span></td>
    <td>${r.failed?'<span style="color:var(--red)">'+r.failed+'</span>':'<span style="color:var(--text2)">0</span>'}</td>
    <td style="color:var(--text2);font-size:11px">${fmt(r.last_active)}</td>
  </tr>`).join('')||'<tr><td colspan="5" style="color:var(--text2);padding:12px">No users</td></tr>';

  // Populate history user dropdown
  const sel=document.getElementById('hist-user-filter');
  const prev=sel.value;
  sel.innerHTML='<option value="">All users</option>'+_stats.map(r=>`<option value="${r.username||''}">${r.username||'(anon)'}</option>`).join('');
  if(prev)sel.value=prev;
}

function renderUsers(rows){
  document.getElementById('users-body').innerHTML=rows.map(r=>`<tr>
    <td style="font-weight:500">${r.username||'(anon)'}</td>
    <td>${r.total}</td>
    <td>${r.active?'<span class="badge running">'+r.active+'</span>':'<span style="color:var(--text2)">0</span>'}</td>
    <td><span style="color:var(--green)">${r.done}</span></td>
    <td>${r.failed?'<span style="color:var(--red)">'+r.failed+'</span>':'<span style="color:var(--text2)">0</span>'}</td>
    <td><span style="color:var(--text2)">${r.cancelled||0}</span></td>
    <td style="color:var(--text2)">${fmt(r.last_active)}</td>
    <td style="display:flex;gap:4px">
      <button class="btn sm ghost" onclick="jumpToJobs('${r.username||''}')">Jobs</button>
      <button class="btn sm ghost" onclick="jumpToHistory('${r.username||''}')">History</button>
    </td>
  </tr>`).join('')||'<tr><td colspan="8" style="color:var(--text2);padding:14px">No users yet</td></tr>';
}

let _allNodes = [];
let _allGpuNodes = [];
let _gpuPage = 0;
const _GPU_PAGE_SIZE = 10;

function gpuPage(dir){
  const pages=Math.ceil(_allGpuNodes.length/_GPU_PAGE_SIZE)||1;
  _gpuPage=Math.max(0,Math.min(_gpuPage+dir,pages-1));
  renderGpuPage();
}
function renderGpuPage(){
  const pages=Math.ceil(_allGpuNodes.length/_GPU_PAGE_SIZE)||1;
  const start=_gpuPage*_GPU_PAGE_SIZE;
  const slice=_allGpuNodes.slice(start,start+_GPU_PAGE_SIZE);
  document.getElementById('gpu-page-label').textContent=`${_gpuPage+1} / ${pages}`;
  document.getElementById('gpu-prev').disabled=_gpuPage===0;
  document.getElementById('gpu-next').disabled=_gpuPage>=pages-1;
  document.getElementById('gpu-body').innerHTML=slice.map(n=>{
    const stateCol=n.state.includes('idle')?'var(--green)':n.state.includes('alloc')?'var(--blue)':n.state.includes('drain')||n.state.includes('down')?'var(--red)':'var(--orange)';
    return `<tr>
      <td style="font-family:monospace;font-size:11px">${n.node}</td>
      <td><span style="color:${stateCol}">${n.state}</span></td>
      <td style="font-size:11px;color:var(--text2)">${n.gres||'—'}</td>
      <td>${n.cpus_alloc}</td>
      <td>${n.cpus_total}</td>
    </tr>`;
  }).join('')||'<tr><td colspan="5" style="color:var(--text2);padding:12px">No GPU nodes or not available</td></tr>';
}

function filterNodes(){
  const f = document.getElementById('node-state-filter').value.toLowerCase();
  const rows = f ? _allNodes.filter(n=>n.state.toLowerCase().includes(f)) : _allNodes;
  renderNodes(rows);
}
function renderNodes(nodes){
  const stateColor={'idle':'var(--green)','allocated':'var(--blue)','mixed':'var(--orange)','drain':'var(--orange)','down':'var(--red)'};
  document.getElementById('node-body').innerHTML=nodes.map(n=>{
    const col=Object.entries(stateColor).find(([k])=>n.state.toLowerCase().includes(k));
    const color=col?col[1]:'var(--text2)';
    const memUsed=n.mem_total_mb&&n.mem_free_mb?Math.round((n.mem_total_mb-n.mem_free_mb)/1024):null;
    const memTotal=n.mem_total_mb?Math.round(n.mem_total_mb/1024):null;
    return `<tr>
      <td style="font-family:monospace;font-size:11px">${n.node}</td>
      <td><span style="color:${color};font-size:11px;font-weight:600">${n.state}</span></td>
      <td>${n.cpus_alloc}</td>
      <td style="color:var(--green)">${n.cpus_idle}</td>
      <td>${n.cpus_total}</td>
      <td>${memTotal!==null?memTotal+'GB':'—'}</td>
      <td>${memUsed!==null?memUsed+'GB':'—'}</td>
      <td style="min-width:100px">${n.cpus_total&&n.cpus_total!='?'?barHTML(n.cpus_alloc,n.cpus_total):'—'}</td>
    </tr>`;
  }).join('')||'<tr><td colspan="8" style="color:var(--text2);padding:12px">No node data</td></tr>';
}

function filterUsers(){
  const q=document.getElementById('user-search').value.toLowerCase();
  renderUsers(q?_stats.filter(r=>(r.username||'').toLowerCase().includes(q)):_stats);
}

function jumpToJobs(u){
  nav('jobs');
  document.getElementById('filter-user').value=u;
  loadJobs();
}

function jumpToHistory(u){
  nav('history');
  const sel=document.getElementById('hist-user-filter');
  if(sel)sel.value=u;
  loadHistory();
}

async function loadPuhti(force=false){
  const data=await api(force?'/admin/refresh-puhti':'/admin/puhti');
  if(!data)return;
  _puhti=data;

  document.getElementById('d-scratch').textContent=data.scratch_total||'?';
  document.getElementById('billing-text').textContent=data.billing||'unavailable';

  const totalCPU=(data.monthly_usage||[]).reduce((s,r)=>s+r.cpu_hours,0);
  document.getElementById('d-cpu').textContent=Math.round(totalCPU);

  // Dashboard queue
  const queue=data.queue||[];
  document.getElementById('d-active-count').textContent=queue.length;
  document.getElementById('q-count').textContent=queue.length;
  const qRows=queue.map(r=>`<tr>
    <td>${r.user}</td><td style="font-family:monospace">${r.job_id}</td>
    <td>${r.partition}</td><td>${B(r.state.toLowerCase())}</td><td>${r.time_limit}</td>
  </tr>`).join()||'<tr><td colspan="5" style="color:var(--text2);padding:12px">Queue empty</td></tr>';
  document.getElementById('d-queue-body').innerHTML=qRows;
  document.getElementById('queue-body').innerHTML=queue.map(r=>`<tr>
    <td>${r.user}</td><td style="font-family:monospace">${r.job_id}</td>
    <td>${r.partition}</td><td>${B(r.state.toLowerCase())}</td><td>${r.time_limit}</td><td style="color:var(--text2)">${r.submit_time||'—'}</td>
  </tr>`).join()||'<tr><td colspan="6" style="color:var(--text2);padding:12px">Queue empty</td></tr>';

  // Partitions
  document.getElementById('partition-body').innerHTML=(data.partitions||[]).map(p=>`<tr>
    <td style="font-weight:600">${p.partition}</td>
    <td>${B(p.available)}</td>
    <td>${p.nodes}</td>
    <td>${p.cpus_alloc}</td>
    <td style="color:var(--green)">${p.cpus_idle}</td>
    <td>${p.cpus_total}</td>
    <td style="min-width:120px">${barHTML(p.cpus_alloc,p.cpus_total)}</td>
  </tr>`).join()||'<tr><td colspan="7" style="color:var(--text2)">No data</td></tr>';

  // Disk & usage
  document.getElementById('disk-body').innerHTML=(data.user_disk||[]).map(r=>`<tr><td>${r.user}</td><td>${r.size}</td></tr>`).join()||'<tr><td colspan="2" style="color:var(--text2);padding:10px">No data</td></tr>';
  document.getElementById('usage-body').innerHTML=(data.monthly_usage||[]).map(r=>`<tr><td>${r.user}</td><td>${r.cpu_hours}</td><td>${r.jobs}</td></tr>`).join()||'<tr><td colspan="3" style="color:var(--text2);padding:10px">No data</td></tr>';

  drawPartitionChart('chart-partitions', data.partitions||[]);

  // Node health
  _allNodes = data.nodes||[];
  document.getElementById('node-count').textContent=_allNodes.length;
  filterNodes();

  // GPU nodes with pagination
  _allGpuNodes = data.gpu_nodes||[];
  _gpuPage = 0;
  document.getElementById('gpu-count').textContent=_allGpuNodes.length;
  renderGpuPage();

  // Pending reasons
  const pr=data.pending_reasons||[];
  document.getElementById('pr-count').textContent=pr.reduce((s,r)=>s+r.count,0)||0;
  document.getElementById('pending-body').innerHTML=pr.map(r=>`<tr>
    <td style="color:var(--orange)">${r.reason}</td><td>${r.count}</td>
  </tr>`).join('')||'<tr><td colspan="2" style="color:var(--text2);padding:10px">No pending jobs</td></tr>';

  // Efficiency
  document.getElementById('eff-body').innerHTML=(data.efficiency||[]).map(r=>{
    const cls=r.efficiency_pct>80?'':'warn';
    const color=r.efficiency_pct>80?'var(--green)':r.efficiency_pct>50?'var(--orange)':'var(--red)';
    return `<tr>
      <td>${r.user}</td>
      <td>${r.jobs}</td>
      <td>${r.cpu_hours_alloc}h</td>
      <td><div class="bar-wrap"><div class="bar"><div class="bar-fill ${cls}" style="width:${Math.min(r.efficiency_pct,100)}%;background:${color}"></div></div><span class="pct-label">${r.efficiency_pct}%</span></div></td>
    </tr>`;
  }).join('')||'<tr><td colspan="4" style="color:var(--text2);padding:10px">No efficiency data yet</td></tr>';
}

async function loadHistory(){
  const data=await api('/admin/history?days=30');
  if(!data)return;
  _history=data.history;
  const total=_history.reduce((s,r)=>s+r.total,0);
  const done=_history.reduce((s,r)=>s+r.done,0);
  const failed=_history.reduce((s,r)=>s+r.failed,0);
  const rate=total?Math.round(done/total*100):0;

  ['h-total','h-done','h-failed','h-rate'].forEach((id,i)=>{
    const el=document.getElementById(id);
    if(el)el.textContent=[total,done,failed,rate+'%'][i];
  });

  document.getElementById('history-body').innerHTML=[..._history].reverse().map(r=>{
    const rt=r.total?Math.round(r.done/r.total*100):0;
    return `<tr>
      <td>${fmtDate(r.day)}</td>
      <td>${r.total}</td>
      <td><span style="color:var(--green)">${r.done}</span></td>
      <td>${r.failed?'<span style="color:var(--red)">'+r.failed+'</span>':'0'}</td>
      <td><span style="color:var(--text2)">${r.cancelled}</span></td>
      <td>${barHTML(r.done,r.total)}</td>
    </tr>`;
  }).join()||'<tr><td colspan="6" style="color:var(--text2);padding:12px">No history yet</td></tr>';

  drawHistoryChart('chart-history',_history);
  drawHistoryChart('chart-history2',_history);

  // Per-user breakdown for the same period (only on History page)
  const hbuBody=document.getElementById('hbu-body');
  if(hbuBody){
    const udata=await api(`/admin/history-by-user?days=${days}`);
    if(udata){
      document.getElementById('hbu-count').textContent=udata.users.length;
      document.getElementById('hbu-period').textContent=`last ${days} days`;
      hbuBody.innerHTML=udata.users.map(r=>{
        const rate=r.total?Math.round(r.done/r.total*100):0;
        return `<tr>
          <td style="font-weight:500">${r.username||'(anon)'}</td>
          <td>${r.total}</td>
          <td><span style="color:var(--green)">${r.done}</span></td>
          <td>${r.failed?'<span style="color:var(--red)">'+r.failed+'</span>':'<span style="color:var(--text2)">0</span>'}</td>
          <td><span style="color:var(--text2)">${r.cancelled}</span></td>
          <td>${barHTML(r.done,r.total)}</td>
          <td style="color:var(--text2);font-size:11px">${fmt(r.last_active)}</td>
          <td><button class="btn sm ghost" onclick="jumpToHistory('${r.username||''}')">Filter</button></td>
        </tr>`;
      }).join('')||'<tr><td colspan="8" style="color:var(--text2);padding:12px">No data</td></tr>';
    }
  }
}

async function loadJobs(){
  const user=document.getElementById('filter-user').value.trim();
  const status=document.getElementById('filter-status').value;
  let path='/admin/jobs?';
  if(user)path+='username='+encodeURIComponent(user)+'&';
  if(status)path+='status='+encodeURIComponent(status);
  const data=await api(path);
  if(!data)return;
  document.getElementById('jobs-count').textContent=data.jobs.length;
  document.getElementById('jobs-body').innerHTML=data.jobs.map(j=>`<tr>
    <td style="font-family:monospace;font-size:10px;color:var(--text2)">${j.job_id.slice(0,8)}</td>
    <td style="font-family:monospace">${j.slurm_id||'—'}</td>
    <td><a href="#" onclick="jumpToJobs('${j.username||''}');return false" style="color:var(--blue);text-decoration:none">${j.username||'—'}</a></td>
    <td>${B(j.status)}</td>
    <td>${j.partition||'—'}</td>
    <td>${j.cpus||'—'}</td>
    <td>${j.memory_gb?j.memory_gb+'GB':'—'}</td>
    <td style="color:var(--text2)">${fmt(j.created)}</td>
  </tr>`).join()||'<tr><td colspan="8" style="color:var(--text2);padding:12px">No jobs found</td></tr>';
}

async function loadContainerRequests(){
  const data=await api('/admin/container-requests');
  if(!data)return;
  const pending=data.requests.filter(r=>r.status==='pending').length;
  document.getElementById('cr-count').textContent=data.requests.length;
  const pendEl=document.getElementById('cr-pending');
  if(pendEl)pendEl.textContent=pending?`${pending} pending approval`:'';
  document.getElementById('containers-body').innerHTML=data.requests.map(r=>`<tr>
    <td>${r.username||'—'}</td>
    <td style="font-weight:600">${r.container}</td>
    <td>${B(r.status)}</td>
    <td><a href="${r.pr_url}" target="_blank" style="color:var(--blue)">PR #${r.pr_number}</a></td>
    <td style="color:var(--text2)">${fmt(r.created)}</td>
  </tr>`).join()||'<tr><td colspan="5" style="color:var(--text2);padding:12px">No requests</td></tr>';
}

function refreshAll(){
  document.getElementById('refresh-ts').textContent='Refreshing…';
  Promise.all([loadStats(),loadPuhti(true),loadHistory(),loadContainerRequests()]).then(()=>{
    document.getElementById('refresh-ts').textContent='Updated '+new Date().toLocaleTimeString();
  });
}

setInterval(()=>{
  const id=document.querySelector('.page.active')?.id;
  if(id==='page-dashboard'){loadStats();loadPuhti();loadHistory();}
  if(id==='page-queue'||id==='page-partitions')loadPuhti();
  if(id==='page-jobs')loadJobs();
  if(id==='page-history')loadHistory();
  if(id==='page-users')loadStats();
  if(id==='page-containers')loadContainerRequests();
  if(id==='page-puhti')loadPuhti();
},900000);

window.addEventListener('resize',()=>{
  if(_history.length)drawHistoryChart('chart-history',_history);
  if(_puhti.partitions)drawPartitionChart('chart-partitions',_puhti.partitions);
});

// Initial load
loadStats();loadPuhti();loadHistory();loadContainerRequests();
document.getElementById('refresh-ts').textContent='Updated '+new Date().toLocaleTimeString();
</script>
</body></html>'''
