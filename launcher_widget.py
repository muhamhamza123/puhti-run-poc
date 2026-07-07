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
            description='↻ Refresh',
            layout=w.Layout(width='100px'),
            button_style='',
        )
        self.req_area = w.Textarea(
            placeholder='numpy\npandas\nmatplotlib\n...',
            description='Requirements:',
            layout=w.Layout(width='400px', height='100px'),
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

        self.refresh_btn.on_click(self._refresh)
        self.run_btn.on_click(self._on_run)

    def show(self):
        display(w.VBox([
            w.HTML('<b style="font-size:14px">Run Notebook on Puhti</b>'),
            w.HBox([self.nb_dd, self.refresh_btn]),
            self.req_area,
            w.HBox([self.partition_dd, self.cpus_sl, self.mem_sl]),
            w.HBox([self.run_btn, self.status_lbl]),
            self.out,
        ]))

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
        }

        try:
            resp = requests.post(f'{API_BASE}/run-notebook', files=files, data=data, timeout=120)
            resp.raise_for_status()
            job = resp.json()
        except Exception as e:
            self.status_lbl.value = f'<span style="color:#ef4444">Submit failed: {e}</span>'
            self.run_btn.disabled = False
            return

        job_id   = job['job_id']
        slurm_id = job['slurm_id']
        self.status_lbl.value = (
            f'<span style="color:#3b82f6">Submitted — Slurm {slurm_id} — polling...</span>'
        )
        self._poll(job_id, slurm_id)

    def _poll(self, job_id, slurm_id):
        import threading
        def _loop():
            try:
              for _ in range(240):
                if _ > 0:
                    time.sleep(5)
                try:
                    r = requests.get(f'{API_BASE}/run-status/{job_id}', timeout=10)
                    status = r.json().get('status', '?')
                except Exception as e:
                    status = '?'
                    self.status_lbl.value = f'<span style="color:#ef4444">Poll error: {e}</span>'

                color = {
                    'queued':  '#f59e0b', 'running': '#3b82f6',
                    'done':    '#10b981', 'failed':  '#ef4444',
                }.get(status, '#64748b')
                self.status_lbl.value = (
                    f'<span style="color:{color}">Slurm {slurm_id} — {status}</span>'
                )

                if status == 'done':
                    self._fetch_results(job_id)
                    self.run_btn.disabled = False
                    return
                if status in ('failed', 'cancelled'):
                    self._show_logs(job_id)
                    self.run_btn.disabled = False
                    return

              self.status_lbl.value = '<span style="color:#ef4444">Timed out after 20 min</span>'
              self.run_btn.disabled = False
            except Exception as e:
              self.status_lbl.value = f'<span style="color:#ef4444">Polling crashed: {e}</span>'
              self.run_btn.disabled = False

        threading.Thread(target=_loop, daemon=True).start()

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
