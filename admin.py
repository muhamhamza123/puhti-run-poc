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
_CACHE_TTL = 60  # seconds


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


_ADMIN_HTML = '''<!doctype html>
<html><head><title>Puhti Admin</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0f172a;color:#f1f5f9;min-height:100vh;font-size:13px}
nav{background:#1e293b;padding:10px 20px;display:flex;align-items:center;gap:12px;border-bottom:1px solid #334155;position:sticky;top:0;z-index:10}
nav h1{font-size:15px;font-weight:700;flex:1}
nav span{font-size:11px;color:#94a3b8}
nav a{font-size:11px;color:#64748b;text-decoration:none;padding:4px 10px;border:1px solid #334155;border-radius:5px}
nav a:hover{color:#f1f5f9;border-color:#64748b}
.tabs{display:flex;background:#1e293b;padding:0 20px;border-bottom:1px solid #334155;gap:2px}
.tab{padding:9px 16px;font-size:12px;cursor:pointer;border:none;background:none;color:#64748b;border-bottom:2px solid transparent;white-space:nowrap}
.tab.active{color:#f1f5f9;border-bottom-color:#3b82f6}
.panel{display:none;padding:20px;max-width:1400px}
.panel.active{display:block}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:20px}
.card{background:#1e293b;border-radius:8px;padding:14px 16px}
.card .val{font-size:26px;font-weight:700;color:#3b82f6;line-height:1}
.card .lbl{font-size:10px;color:#64748b;margin-top:5px;text-transform:uppercase;letter-spacing:.5px}
.section{margin-bottom:24px}
.section-title{font-size:12px;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.6px;margin-bottom:8px;padding-bottom:6px;border-bottom:1px solid #1e293b}
.tbl-wrap{overflow-x:auto;border-radius:8px;border:1px solid #1e293b}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:7px 10px;color:#64748b;font-weight:600;background:#1e293b;font-size:10px;text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}
td{padding:6px 10px;border-top:1px solid #1e293b;white-space:nowrap}
tr:hover td{background:#1a2744}
.badge{padding:1px 7px;border-radius:20px;font-size:10px;font-weight:700;display:inline-block}
.badge.done,.badge.up,.badge.merged{background:#064e3b;color:#34d399}
.badge.failed,.badge.down,.badge.closed{background:#450a0a;color:#f87171}
.badge.queued,.badge.pending{background:#431407;color:#fb923c}
.badge.running{background:#1e3a5f;color:#60a5fa}
.badge.cancelled{background:#1e293b;color:#64748b}
.filters{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center}
.filters label{font-size:11px;color:#64748b}
input,select{background:#1e293b;border:1px solid #334155;color:#f1f5f9;padding:5px 9px;border-radius:5px;font-size:12px;height:30px}
input:focus,select:focus{outline:none;border-color:#3b82f6}
button.act{background:#3b82f6;color:white;border:none;padding:5px 12px;border-radius:5px;font-size:11px;cursor:pointer;height:30px}
button.act:hover{background:#2563eb}
button.act.sm{height:24px;padding:3px 8px;font-size:10px}
.refresh-info{font-size:10px;color:#475569;display:flex;align-items:center;gap:8px;margin-bottom:14px}
pre.billing{background:#1e293b;border-radius:6px;padding:10px 12px;font-size:11px;color:#94a3b8;white-space:pre-wrap;border:1px solid #334155}
.search-row{display:flex;gap:8px;margin-bottom:10px}
.search-row input{flex:1;max-width:280px}
.count-badge{background:#334155;color:#94a3b8;padding:2px 8px;border-radius:20px;font-size:10px;font-weight:600}
</style>
</head><body>

<nav>
  <h1>⚡ Puhti Admin</h1>
  <span>Signed in as <strong>__USER__</strong></span>
  <a href="__BASE__/admin/logout">Sign out</a>
</nav>

<div class="tabs">
  <button class="tab active" onclick="showTab('overview')">Overview</button>
  <button class="tab" onclick="showTab('puhti')">Puhti System</button>
  <button class="tab" onclick="showTab('jobs')">All Jobs</button>
  <button class="tab" onclick="showTab('containers')">Container Requests</button>
</div>

<!-- OVERVIEW -->
<div id="tab-overview" class="panel active">
  <div class="grid">
    <div class="card"><div class="val" id="ov-total">…</div><div class="lbl">Total Jobs</div></div>
    <div class="card"><div class="val" id="ov-active" style="color:#60a5fa">…</div><div class="lbl">Active Now</div></div>
    <div class="card"><div class="val" id="ov-done" style="color:#34d399">…</div><div class="lbl">Completed</div></div>
    <div class="card"><div class="val" id="ov-failed" style="color:#f87171">…</div><div class="lbl">Failed</div></div>
    <div class="card"><div class="val" id="ov-users">…</div><div class="lbl">Total Users</div></div>
    <div class="card"><div class="val" id="ov-pending-pr" style="color:#fb923c">…</div><div class="lbl">Pending PRs</div></div>
    <div class="card"><div class="val" id="ov-scratch" style="color:#a78bfa">…</div><div class="lbl">Scratch Used</div></div>
    <div class="card"><div class="val" id="ov-cpu-hours" style="color:#fb923c">…</div><div class="lbl">CPU Hours (month)</div></div>
  </div>

  <div class="section">
    <div class="section-title">Users <span class="count-badge" id="user-count">0</span></div>
    <div class="search-row">
      <input id="user-search" placeholder="Search users…" oninput="filterUsers()">
    </div>
    <div class="tbl-wrap">
    <table>
      <thead><tr><th>User</th><th>Total</th><th>Active</th><th>Done</th><th>Failed</th><th>Cancelled</th><th>Last Active</th></tr></thead>
      <tbody id="stats-body"></tbody>
    </table>
    </div>
  </div>
</div>

<!-- PUHTI SYSTEM -->
<div id="tab-puhti" class="panel">
  <div class="refresh-info">
    <span id="puhti-age">Loading…</span>
    <button class="act sm" onclick="refreshPuhti()">↻ Force refresh</button>
  </div>

  <div class="section">
    <div class="section-title">Billing Units — project_2014823</div>
    <pre class="billing" id="billing-text">Loading…</pre>
  </div>

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:24px">
    <div class="section">
      <div class="section-title">Scratch Disk by User</div>
      <div class="tbl-wrap">
      <table>
        <thead><tr><th>User</th><th>Disk Used</th></tr></thead>
        <tbody id="disk-body"></tbody>
      </table>
      </div>
    </div>
    <div class="section">
      <div class="section-title">CPU Hours This Month</div>
      <div class="tbl-wrap">
      <table>
        <thead><tr><th>User</th><th>CPU Hours</th><th>Jobs</th></tr></thead>
        <tbody id="usage-body"></tbody>
      </table>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="section-title">Current Queue — project_2014823 <span class="count-badge" id="queue-count">0</span></div>
    <div class="tbl-wrap">
    <table>
      <thead><tr><th>User</th><th>Slurm Job</th><th>Partition</th><th>State</th><th>Time Limit</th></tr></thead>
      <tbody id="queue-body"></tbody>
    </table>
    </div>
  </div>

  <div class="section">
    <div class="section-title">Partition Status</div>
    <div class="tbl-wrap">
    <table>
      <thead><tr><th>Partition</th><th>Status</th><th>Nodes</th><th>CPUs Alloc</th><th>CPUs Idle</th><th>CPUs Total</th><th>% Used</th></tr></thead>
      <tbody id="partition-body"></tbody>
    </table>
    </div>
  </div>
</div>

<!-- ALL JOBS -->
<div id="tab-jobs" class="panel">
  <div class="filters">
    <input id="filter-user" placeholder="Filter by username…" oninput="loadJobs()" style="max-width:200px">
    <select id="filter-status" onchange="loadJobs()">
      <option value="">All statuses</option>
      <option>queued</option><option>running</option>
      <option>done</option><option>failed</option><option>cancelled</option>
    </select>
    <span class="count-badge" id="jobs-count">0</span>
  </div>
  <div class="tbl-wrap">
  <table>
    <thead><tr><th>Job ID</th><th>Slurm</th><th>User</th><th>Status</th><th>Partition</th><th>CPUs</th><th>RAM</th><th>Submitted</th></tr></thead>
    <tbody id="jobs-body"></tbody>
  </table>
  </div>
</div>

<!-- CONTAINER REQUESTS -->
<div id="tab-containers" class="panel">
  <div class="section-title" style="margin-bottom:12px">Container Requests <span class="count-badge" id="cr-count">0</span></div>
  <div class="tbl-wrap">
  <table>
    <thead><tr><th>User</th><th>Container</th><th>Status</th><th>PR</th><th>Requested</th></tr></thead>
    <tbody id="containers-body"></tbody>
  </table>
  </div>
</div>

<script>
const BASE = '__BASE__';
let _allStats = [];

function showTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  const idx = ['overview','puhti','jobs','containers'].indexOf(name);
  document.querySelectorAll('.tab')[idx].classList.add('active');
  document.getElementById('tab-'+name).classList.add('active');
  if (name==='overview') { loadStats(); loadPuhti(); loadContainerRequests(); }
  if (name==='puhti') loadPuhti();
  if (name==='jobs') loadJobs();
  if (name==='containers') loadContainerRequests();
}

async function api(path) {
  const r = await fetch(BASE+path, {credentials:'include'});
  if (r.status===302||r.status===401) { location.href=BASE+'/admin/login'; return null; }
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

const badge = s => `<span class="badge ${s}">${s}</span>`;
const fmt = dt => dt ? new Date(dt.replace(' ','T')+'Z').toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '—';
const pct = (a,t) => t>0 ? Math.round(a/t*100)+'%' : '—';

function filterUsers() {
  const q = document.getElementById('user-search').value.toLowerCase();
  const rows = _allStats.filter(r => !q || (r.username||'').toLowerCase().includes(q));
  renderStats(rows);
}

function renderStats(rows) {
  document.getElementById('stats-body').innerHTML = rows.map(r => `<tr>
    <td><a href="#" onclick="jumpToJobs('${r.username||''}');return false" style="color:#60a5fa;text-decoration:none">${r.username||'(anon)'}</a></td>
    <td>${r.total}</td>
    <td>${r.active ? badge('running')+' '+r.active : '<span style="color:#475569">0</span>'}</td>
    <td><span style="color:#34d399">${r.done}</span></td>
    <td>${r.failed ? '<span style="color:#f87171">'+r.failed+'</span>' : '<span style="color:#475569">0</span>'}</td>
    <td><span style="color:#475569">${r.cancelled||0}</span></td>
    <td style="color:#64748b">${fmt(r.last_active)}</td>
  </tr>`).join('') || '<tr><td colspan="7" style="color:#475569;padding:12px">No users yet</td></tr>';
}

function jumpToJobs(username) {
  showTab('jobs');
  document.getElementById('filter-user').value = username;
  loadJobs();
}

async function loadStats() {
  const data = await api('/admin/stats');
  if (!data) return;
  _allStats = data.stats;
  document.getElementById('ov-users').textContent = _allStats.length;
  document.getElementById('ov-total').textContent = _allStats.reduce((s,r)=>s+r.total,0);
  document.getElementById('ov-active').textContent = _allStats.reduce((s,r)=>s+r.active,0);
  document.getElementById('ov-done').textContent = _allStats.reduce((s,r)=>s+r.done,0);
  document.getElementById('ov-failed').textContent = _allStats.reduce((s,r)=>s+r.failed,0);
  document.getElementById('user-count').textContent = _allStats.length;
  filterUsers();
}

async function loadPuhti(force=false) {
  const data = await api(force?'/admin/refresh-puhti':'/admin/puhti');
  if (!data) return;

  document.getElementById('puhti-age').textContent = 'Last updated: '+new Date().toLocaleTimeString()+' (cache 60s)';
  document.getElementById('ov-scratch').textContent = data.scratch_total||'?';
  document.getElementById('billing-text').textContent = data.billing||'unavailable';

  const totalCPU = (data.monthly_usage||[]).reduce((s,r)=>s+r.cpu_hours,0);
  document.getElementById('ov-cpu-hours').textContent = Math.round(totalCPU);

  document.getElementById('disk-body').innerHTML =
    (data.user_disk||[]).map(r=>`<tr><td>${r.user}</td><td>${r.size}</td></tr>`).join('') ||
    '<tr><td colspan="2" style="color:#475569">No data</td></tr>';

  document.getElementById('usage-body').innerHTML =
    (data.monthly_usage||[]).map(r=>`<tr><td>${r.user}</td><td>${r.cpu_hours}</td><td>${r.jobs}</td></tr>`).join('') ||
    '<tr><td colspan="3" style="color:#475569">No data</td></tr>';

  const queue = data.queue||[];
  document.getElementById('queue-count').textContent = queue.length;
  document.getElementById('queue-body').innerHTML = queue.map(r=>`<tr>
    <td>${r.user}</td><td style="font-family:monospace">${r.job_id}</td>
    <td>${r.partition}</td><td>${badge(r.state.toLowerCase())}</td><td>${r.time_limit}</td>
  </tr>`).join('') || '<tr><td colspan="5" style="color:#475569;padding:10px">Queue empty</td></tr>';

  document.getElementById('partition-body').innerHTML =
    (data.partitions||[]).map(r=>`<tr>
      <td style="font-weight:600">${r.partition}</td>
      <td>${badge(r.available)}</td>
      <td>${r.nodes}</td>
      <td>${r.cpus_alloc}</td>
      <td style="color:#34d399">${r.cpus_idle}</td>
      <td>${r.cpus_total}</td>
      <td><div style="background:#334155;border-radius:4px;height:6px;width:80px;overflow:hidden">
        <div style="background:#3b82f6;height:100%;width:${pct(r.cpus_alloc,r.cpus_total)}"></div></div>
        <span style="font-size:10px;color:#64748b">${pct(r.cpus_alloc,r.cpus_total)}</span>
      </td>
    </tr>`).join('') || '<tr><td colspan="7" style="color:#475569">No data</td></tr>';
}

async function refreshPuhti() { await loadPuhti(true); }

async function loadJobs() {
  const user = document.getElementById('filter-user').value.trim();
  const status = document.getElementById('filter-status').value;
  let path = '/admin/jobs?';
  if (user) path += 'username='+encodeURIComponent(user)+'&';
  if (status) path += 'status='+encodeURIComponent(status);
  const data = await api(path);
  if (!data) return;
  document.getElementById('jobs-count').textContent = data.jobs.length;
  document.getElementById('jobs-body').innerHTML = data.jobs.map(j=>`<tr>
    <td style="font-family:monospace;font-size:10px;color:#64748b">${j.job_id.slice(0,8)}</td>
    <td style="font-family:monospace">${j.slurm_id||'—'}</td>
    <td><a href="#" onclick="jumpToJobs('${j.username||''}');return false" style="color:#60a5fa;text-decoration:none">${j.username||'—'}</a></td>
    <td>${badge(j.status)}</td>
    <td>${j.partition||'—'}</td>
    <td>${j.cpus||'—'}</td>
    <td>${j.memory_gb?j.memory_gb+'GB':'—'}</td>
    <td style="color:#64748b">${fmt(j.created)}</td>
  </tr>`).join('') || '<tr><td colspan="8" style="color:#475569;padding:12px">No jobs found</td></tr>';
}

async function loadContainerRequests() {
  const data = await api('/admin/container-requests');
  if (!data) return;
  const pending = data.requests.filter(r=>r.status==='pending').length;
  document.getElementById('ov-pending-pr').textContent = pending;
  document.getElementById('cr-count').textContent = data.requests.length;
  document.getElementById('containers-body').innerHTML =
    data.requests.map(r=>`<tr>
      <td>${r.username||'—'}</td>
      <td style="font-weight:600">${r.container}</td>
      <td>${badge(r.status)}</td>
      <td><a href="${r.pr_url}" target="_blank" style="color:#60a5fa">PR #${r.pr_number}</a></td>
      <td style="color:#64748b">${fmt(r.created)}</td>
    </tr>`).join('') || '<tr><td colspan="5" style="color:#475569;padding:12px">No requests</td></tr>';
}

setInterval(()=>{
  const id = document.querySelector('.panel.active')?.id;
  if (id==='tab-overview') { loadStats(); loadPuhti(); }
  if (id==='tab-puhti') loadPuhti();
  if (id==='tab-jobs') loadJobs();
  if (id==='tab-containers') loadContainerRequests();
}, 60000);

loadStats(); loadPuhti(); loadContainerRequests();
</script>
</body></html>'''
