"""
Drop this into any JupyterHub notebook cell to get a full "Run on Puhti" widget.

Usage (one cell, no files needed):
    from run_button_widget import PuhtiRunWidget
    PuhtiRunWidget().show()

User types code and requirements directly in the widget text areas.
"""
import ipywidgets as w
from IPython.display import display
import requests, time, os, zipfile, io

API_BASE = 'http://hbv.we3data.com:8002'

PLACEHOLDER_CODE = '''\
import os
import numpy as np

os.makedirs('output', exist_ok=True)

data = np.random.randn(1000)
print(f'Mean: {data.mean():.4f}')
np.save('output/data.npy', data)
'''.strip()

PLACEHOLDER_REQS = 'numpy'


class PuhtiRunWidget:
    def __init__(self):
        # ── Code editor ──────────────────────────────────────────────────────
        self.code_area = w.Textarea(
            value=PLACEHOLDER_CODE,
            placeholder='Write your Python code here...',
            layout=w.Layout(width='100%', height='220px',
                            font_family='monospace'),
        )
        self.req_area = w.Textarea(
            value=PLACEHOLDER_REQS,
            placeholder='numpy\npandas\n...',
            layout=w.Layout(width='100%', height='80px'),
        )

        # ── Controls ─────────────────────────────────────────────────────────
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
        self.status_lbl = w.HTML(value='<span style="color:#64748b">Ready</span>')
        self.out = w.Output()

        self.run_btn.on_click(self._on_run)

    def show(self):
        display(w.VBox([
            w.HTML('<b>Code</b>'),
            self.code_area,
            w.HTML('<b>requirements.txt</b> (one package per line)'),
            self.req_area,
            w.HBox([self.partition_dd, self.cpus_sl, self.mem_sl]),
            w.HBox([self.run_btn, self.status_lbl]),
            self.out,
        ]))

    def _on_run(self, _):
        self.run_btn.disabled = True
        self.status_lbl.value = '<span style="color:#f59e0b">Submitting...</span>'
        self.out.clear_output()

        files = {
            'script': ('script.py', self.code_area.value.encode(), 'text/plain'),
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
            resp = requests.post(f'{API_BASE}/run-code', files=files, data=data, timeout=120)
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

    def _poll(self, job_id: str, slurm_id: str):
        import threading
        def _loop():
            for _ in range(240):  # poll up to 20 minutes
                time.sleep(5)
                try:
                    r = requests.get(f'{API_BASE}/run-status/{job_id}', timeout=10)
                    status = r.json().get('status', '?')
                except Exception:
                    status = '?'

                color = {
                    'queued':    '#f59e0b',
                    'running':   '#3b82f6',
                    'done':      '#10b981',
                    'failed':    '#ef4444',
                    'cancelled': '#64748b',
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

        threading.Thread(target=_loop, daemon=True).start()

    def _fetch_results(self, job_id: str):
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

    def _show_logs(self, job_id: str):
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
