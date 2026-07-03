"""
Drop this cell into any JupyterHub notebook to get a "Run on Puhti" button.

Usage:
    from run_button_widget import PuhtiRunWidget
    PuhtiRunWidget(script_path='my_analysis.py', requirements_path='requirements.txt').show()
"""
import ipywidgets as w
from IPython.display import display
import requests, time, os, zipfile, io

API_BASE = 'http://hbv.we3data.com:8001'  # head node API


class PuhtiRunWidget:
    def __init__(self, script_path: str, requirements_path: str = None,
                 input_files: list = None):
        self.script_path       = script_path
        self.requirements_path = requirements_path
        self.input_files       = input_files or []

        # ── UI elements ──────────────────────────────────────────────────────
        self.partition_dd = w.Dropdown(
            options=['small', 'large', 'gpu', 'gpumedium', 'longrun'],
            value='small',
            description='Partition:',
            layout=w.Layout(width='200px'),
        )
        self.cpus_sl = w.IntSlider(value=4, min=1, max=40, description='CPUs:',
                                   layout=w.Layout(width='300px'))
        self.mem_sl  = w.IntSlider(value=16, min=1, max=382, description='RAM (GB):',
                                   layout=w.Layout(width='300px'))
        self.run_btn = w.Button(description='▶  Run on Puhti',
                                button_style='success',
                                layout=w.Layout(width='180px'))
        self.status_lbl = w.HTML(value='<span style="color:#64748b">Ready</span>')
        self.out = w.Output()

        self.run_btn.on_click(self._on_run)

    def show(self):
        display(w.VBox([
            w.HBox([self.partition_dd, self.cpus_sl, self.mem_sl]),
            w.HBox([self.run_btn, self.status_lbl]),
            self.out,
        ]))

    def _on_run(self, _):
        self.run_btn.disabled = True
        self.status_lbl.value = '<span style="color:#f59e0b">Submitting...</span>'
        self.out.clear_output()

        files = {}
        with open(self.script_path, 'rb') as f:
            files['script'] = ('script.py', f.read(), 'text/plain')
        if self.requirements_path and os.path.exists(self.requirements_path):
            with open(self.requirements_path, 'rb') as f:
                files['requirements'] = ('requirements.txt', f.read(), 'text/plain')
        for path in self.input_files:
            with open(path, 'rb') as f:
                files[f'inputs'] = (os.path.basename(path), f.read())

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
            f'<span style="color:#3b82f6">Submitted — Slurm job {slurm_id} — polling...</span>'
        )

        # Poll status
        self._poll(job_id, slurm_id)

    def _poll(self, job_id: str, slurm_id: str):
        import threading
        def _loop():
            for _ in range(120):   # poll up to 10 minutes
                time.sleep(5)
                try:
                    r = requests.get(f'{API_BASE}/run-status/{job_id}', timeout=10)
                    status = r.json().get('status', '?')
                except Exception:
                    status = '?'

                color = {'queued': '#f59e0b', 'running': '#3b82f6',
                         'done': '#10b981', 'failed': '#ef4444'}.get(status, '#64748b')
                self.status_lbl.value = (
                    f'<span style="color:{color}">Slurm {slurm_id} — {status}</span>'
                )

                if status == 'done':
                    self._fetch_results(job_id)
                    self.run_btn.disabled = False
                    return
                if status == 'failed':
                    self._show_stderr(job_id)
                    self.run_btn.disabled = False
                    return

            self.status_lbl.value = '<span style="color:#ef4444">Timed out waiting</span>'
            self.run_btn.disabled = False

        threading.Thread(target=_loop, daemon=True).start()

    def _fetch_results(self, job_id: str):
        with self.out:
            try:
                r = requests.get(f'{API_BASE}/run-results/{job_id}', timeout=30)
                r.raise_for_status()
                z = zipfile.ZipFile(io.BytesIO(r.content))
                os.makedirs('puhti_output', exist_ok=True)
                z.extractall('puhti_output')
                print(f'Results saved to ./puhti_output/:')
                for name in z.namelist():
                    print(f'  {name}')
            except Exception as e:
                print(f'Could not fetch results: {e}')

    def _show_stderr(self, job_id: str):
        with self.out:
            try:
                r = requests.get(f'{API_BASE}/run-logs/{job_id}', timeout=10)
                print('--- stderr ---')
                print(r.text[:2000])
            except Exception:
                print('Job failed. Check logs on Puhti.')
