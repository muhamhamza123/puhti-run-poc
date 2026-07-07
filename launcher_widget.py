"""
PuhtiLauncher — submit any .ipynb notebook to Puhti from JupyterHub.

Usage (one cell):
    from launcher_widget import PuhtiLauncher
    PuhtiLauncher().show()

The widget lists all notebooks in the current directory.
User picks one, sets resources, clicks Run on Puhti.
The API converts the notebook to a script and runs it on Puhti.
"""
import ipywidgets as w
from IPython.display import display
import requests, time, os, zipfile, io, glob

API_BASE = 'http://hbv.we3data.com:8002'
CONTAINERS_ENDPOINT = f'{API_BASE}/containers'


def _fetch_containers():
    try:
        r = requests.get(CONTAINERS_ENDPOINT, timeout=10)
        if r.ok:
            return r.json().get('containers', ['general-compute'])
    except Exception:
        pass
    return ['general-compute']


def _find_notebooks():
    nbs = sorted(glob.glob('*.ipynb'))
    # exclude the launcher notebook itself
    return [nb for nb in nbs if 'launcher' not in nb.lower() and 'Untitled' not in nb] or nbs


class PuhtiLauncher:
    def __init__(self):
        notebooks = _find_notebooks()

        self.nb_dd = w.Dropdown(
            options=notebooks if notebooks else ['(no notebooks found)'],
            description='Notebook:',
            layout=w.Layout(width='400px'),
        )
        self.refresh_btn = w.Button(
            description='↻ Notebooks',
            layout=w.Layout(width='120px'),
            button_style='',
        )
        self.status_btn = w.Button(
            description='⟳ Check Status',
            layout=w.Layout(width='140px'),
            button_style='info',
            disabled=True,
        )
        self.req_area = w.Textarea(
            placeholder='numpy\npandas\nmatplotlib\n...',
            description='Requirements:',
            layout=w.Layout(width='400px', height='100px'),
        )
        self.container_dd = w.Dropdown(
            options=_fetch_containers(),
            value=_fetch_containers()[0],
            description='Container:',
            layout=w.Layout(width='260px'),
        )
        self.partition_dd = w.Dropdown(
            options=['small', 'large', 'gpu', 'gpumedium', 'longrun'],
            value='small',
            description='Partition:',
            layout=w.Layout(width='200px'),
        )
        self.cpus_sl = w.IntSlider(
            value=4, min=1, max=40, description='CPUs:',
            layout=w.Layout(width='300px'),
        )
        self.mem_sl = w.IntSlider(
            value=16, min=1, max=382, description='RAM (GB):',
            layout=w.Layout(width='300px'),
        )
        self.run_btn = w.Button(
            description='▶  Run on Puhti',
            button_style='success',
            layout=w.Layout(width='180px'),
        )
        self.status_lbl = w.HTML('<span style="color:#64748b">Ready</span>')
        self.out = w.Output()

        # Container request section
        self.def_upload_btn = w.Button(
            description='📁 Choose .def file',
            layout=w.Layout(width='160px'),
        )
        self.def_file_lbl = w.Label('No file selected')
        self.def_desc = w.Text(
            placeholder='Brief description (optional)',
            layout=w.Layout(width='350px'),
        )
        self.request_btn = w.Button(
            description='Request Container',
            button_style='warning',
            layout=w.Layout(width='180px'),
            disabled=True,
        )
        self.request_out = w.Output()
        self._def_path = None

        self._job_id   = None
        self._slurm_id = None
        self.refresh_btn.on_click(self._on_refresh)
        self.status_btn.on_click(self._check_status)
        self.run_btn.on_click(self._on_run)
        self.def_upload_btn.on_click(self._pick_def)
        self.request_btn.on_click(self._on_request)

    def show(self):
        display(w.VBox([
            w.HTML('<b style="font-size:14px">Run Notebook on Puhti</b>'),
            w.HBox([self.nb_dd, self.refresh_btn]),
            self.req_area,
            w.HBox([self.container_dd, self.partition_dd]),
            w.HBox([self.cpus_sl, self.mem_sl]),
            w.HBox([self.run_btn, self.status_btn, self.status_lbl]),
            self.out,
            w.HTML('<hr><b style="font-size:13px">Request New Container</b>'),
            w.HBox([self.def_upload_btn, self.def_file_lbl]),
            self.def_desc,
            self.request_btn,
            self.request_out,
        ]))

    def _on_refresh(self, _):
        nbs = _find_notebooks()
        self.nb_dd.options = nbs if nbs else ['(no notebooks found)']

    def _refresh(self, _):
        nbs = _find_notebooks()
        self.nb_dd.options = nbs if nbs else ['(no notebooks found)']

    def _on_run(self, _):
        nb_path = self.nb_dd.value
        if not os.path.exists(nb_path):
            self.status_lbl.value = f'<span style="color:#ef4444">File not found: {nb_path}</span>'
            return

        self.run_btn.disabled = True
        self.status_lbl.value = '<span style="color:#f59e0b">Submitting...</span>'
        self.out.clear_output()

        with open(nb_path, 'rb') as f:
            nb_bytes = f.read()

        files = {
            'notebook': (nb_path, nb_bytes, 'application/json'),
        }
        reqs = self.req_area.value.strip()
        if reqs:
            files['requirements'] = ('requirements.txt', reqs.encode(), 'text/plain')

        data = {
            'partition': self.partition_dd.value,
            'cpus':      str(self.cpus_sl.value),
            'memory_gb': str(self.mem_sl.value),
            'container': self.container_dd.value,
        }

        try:
            resp = requests.post(f'{API_BASE}/run-notebook', files=files, data=data, timeout=120)
            resp.raise_for_status()
            job = resp.json()
        except Exception as e:
            self.status_lbl.value = f'<span style="color:#ef4444">Submit failed: {e}</span>'
            self.run_btn.disabled = False
            return

        self._job_id   = job['job_id']
        self._slurm_id = job['slurm_id']
        self.status_lbl.value = (
            f'<span style="color:#3b82f6">Submitted — Slurm {self._slurm_id}</span>'
        )
        self.run_btn.disabled = False
        self.status_btn.disabled = False

    def _check_status(self, _=None):
        job_id   = getattr(self, '_job_id', None)
        slurm_id = getattr(self, '_slurm_id', '?')
        if not job_id:
            return
        try:
            r = requests.get(f'{API_BASE}/run-status/{job_id}', timeout=60)
            status = r.json().get('status', '?')
        except Exception as e:
            self.status_lbl.value = f'<span style="color:#ef4444">Poll error: {e}</span>'
            return

        color = {
            'queued':  '#f59e0b', 'running': '#3b82f6',
            'done':    '#10b981', 'failed':  '#ef4444',
        }.get(status, '#64748b')
        self.status_lbl.value = (
            f'<span style="color:{color}">Slurm {slurm_id} — {status}</span>'
        )

        if status == 'done':
            self._fetch_results(job_id)
            self.status_btn.disabled = True
            self._job_id = None
        elif status in ('failed', 'cancelled'):
            self._show_logs(job_id)
            self.status_btn.disabled = True
            self._job_id = None

    def _fetch_results(self, job_id):
        with self.out:
            try:
                r = requests.get(f'{API_BASE}/run-results/{job_id}', timeout=60)
                r.raise_for_status()
                z = zipfile.ZipFile(io.BytesIO(r.content))
                os.makedirs('puhti_output', exist_ok=True)
                z.extractall('puhti_output')
                print('Results saved to ./puhti_output/:')
                for name in z.namelist():
                    print(f'  {name}')
            except Exception as e:
                print(f'Could not fetch results: {e}')

    def _pick_def(self, _):
        import glob
        defs = sorted(glob.glob('*.def'))
        if not defs:
            self.def_file_lbl.value = 'No .def files found in current directory'
            return
        # cycle through available .def files on each click
        if self._def_path in defs:
            idx = (defs.index(self._def_path) + 1) % len(defs)
        else:
            idx = 0
        self._def_path = defs[idx]
        self.def_file_lbl.value = self._def_path
        self.request_btn.disabled = False

    def _on_request(self, _):
        if not self._def_path or not os.path.exists(self._def_path):
            with self.request_out:
                print('No .def file selected')
            return
        self.request_btn.disabled = True
        self.request_out.clear_output()
        with self.request_out:
            try:
                with open(self._def_path, 'rb') as f:
                    resp = requests.post(
                        f'{API_BASE}/request-container',
                        files={'def_file': (self._def_path, f, 'text/plain')},
                        data={'description': self.def_desc.value.strip()},
                        timeout=30,
                    )
                resp.raise_for_status()
                result = resp.json()
                print(f'PR opened: {result["pr_url"]}')
                print(f'Container name will be: {result["container_name"]}')
                print('Once the PR is merged, the container will build automatically.')
            except Exception as e:
                print(f'Request failed: {e}')
            finally:
                self.request_btn.disabled = False

    def _show_logs(self, job_id):
        with self.out:
            try:
                r = requests.get(f'{API_BASE}/run-logs/{job_id}', timeout=15)
                logs = r.json()
                print('--- stdout ---')
                print(logs.get('stdout', '(empty)'))
                print('--- stderr ---')
                print(logs.get('stderr', '(empty)'))
            except Exception:
                print('Job failed. Could not fetch logs.')
