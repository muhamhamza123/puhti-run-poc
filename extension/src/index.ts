import {
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';
import { ICommandPalette } from '@jupyterlab/apputils';
import { INotebookTracker } from '@jupyterlab/notebook';
import { Widget } from '@lumino/widgets';

const API = 'http://hbv.we3data.com:8002';

// ── helpers ──────────────────────────────────────────────────────────────────

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  css: string,
  text?: string
): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  e.style.cssText = css;
  if (text !== undefined) {
    (e as HTMLElement).textContent = text;
  }
  return e;
}

function btn(label: string, color: string, onClick: () => void): HTMLButtonElement {
  const b = el(
    'button',
    `padding:5px 12px;border-radius:6px;border:none;background:${color};color:white;` +
      'cursor:pointer;font-size:12px;font-family:inherit;font-weight:500;'
  );
  b.textContent = label;
  b.onclick = onClick;
  return b;
}

async function api(method: string, path: string, body?: FormData | string): Promise<any> {
  const opts: RequestInit = { method };
  if (body instanceof FormData) {
    opts.body = body;
  } else if (body) {
    opts.body = body;
    opts.headers = { 'Content-Type': 'application/json' };
  }
  const r = await fetch(`${API}${path}`, opts);
  if (!r.ok) {
    const t = await r.text();
    throw new Error(t);
  }
  return r.json();
}

// ── Main widget ──────────────────────────────────────────────────────────────

class PuhtiWidget extends Widget {
  private _tracker: INotebookTracker;
  private _jobId: string | null = null;
  private _slurmId: string | null = null;

  // submit tab refs
  private _nbSelect!: HTMLSelectElement;
  private _containerSelect!: HTMLSelectElement;
  private _partitionSelect!: HTMLSelectElement;
  private _cpuRange!: HTMLInputElement;
  private _cpuLabel!: HTMLSpanElement;
  private _memRange!: HTMLInputElement;
  private _memLabel!: HTMLSpanElement;
  private _reqText!: HTMLTextAreaElement;
  private _submitStatus!: HTMLDivElement;

  // jobs tab refs
  private _jobsList!: HTMLDivElement;
  private _statusLabel!: HTMLDivElement;
  private _resultsOut!: HTMLDivElement;

  // containers tab refs
  private _containersList!: HTMLDivElement;
  private _defInput!: HTMLInputElement;
  private _defFilename!: HTMLSpanElement;
  private _defBytes: Uint8Array | null = null;
  private _defFname = '';
  private _defDesc!: HTMLInputElement;
  private _requestStatus!: HTMLDivElement;

  constructor(tracker: INotebookTracker) {
    super();
    this._tracker = tracker;
    this.id = 'puhti-panel';
    this.title.label = 'Puhti';
    this.title.caption = 'Run notebooks on Puhti';
    this.title.closable = true;
    this.node.style.cssText =
      'display:flex;flex-direction:column;height:100%;' +
      'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;' +
      'background:var(--jp-layout-color1);color:var(--jp-ui-font-color1);overflow:hidden;';
    this._build();
  }

  private _build(): void {
    // Header
    const hdr = el(
      'div',
      'background:var(--jp-layout-color2);border-bottom:1px solid var(--jp-border-color2);' +
        'padding:10px 12px;flex-shrink:0;'
    );
    const title = el('span', 'font-size:13px;font-weight:700;', '⚡ Puhti Runner');
    hdr.appendChild(title);
    this.node.appendChild(hdr);

    // Tab bar
    const tabBar = el(
      'div',
      'display:flex;border-bottom:1px solid var(--jp-border-color2);flex-shrink:0;'
    );
    const tabs = ['Submit', 'Jobs', 'Containers'];
    const panels: HTMLDivElement[] = [];

    tabs.forEach((name, i) => {
      const t = el(
        'button',
        'flex:1;padding:8px 4px;border:none;background:none;cursor:pointer;font-size:12px;' +
          'font-family:inherit;border-bottom:2px solid transparent;color:var(--jp-ui-font-color2);',
        name
      );
      const p = el('div', 'flex:1;overflow-y:auto;padding:12px;display:none;flex-direction:column;gap:10px;') as HTMLDivElement;
      panels.push(p);

      t.onclick = () => {
        panels.forEach((pp, j) => {
          pp.style.display = 'none';
          (tabBar.children[j] as HTMLElement).style.borderBottomColor = 'transparent';
          (tabBar.children[j] as HTMLElement).style.color = 'var(--jp-ui-font-color2)';
        });
        p.style.display = 'flex';
        t.style.borderBottomColor = 'var(--jp-brand-color1)';
        t.style.color = 'var(--jp-ui-font-color1)';
        if (i === 0) { this._refreshNotebooks(); }
        if (i === 1) { this._refreshJobs(); }
        if (i === 2) { this._refreshContainers(); }
      };
      tabBar.appendChild(t);
      this.node.appendChild(p);
    });
    this.node.insertBefore(tabBar, panels[0]);

    // Build each panel
    this._buildSubmit(panels[0]);
    this._buildJobs(panels[1]);
    this._buildContainers(panels[2]);

    // Activate first tab
    (tabBar.children[0] as HTMLElement).click();
  }

  // ── Submit tab ──────────────────────────────────────────────────────────────

  private _buildSubmit(p: HTMLDivElement): void {
    p.appendChild(this._label('Notebook'));
    this._nbSelect = el('select', this._inputCss()) as HTMLSelectElement;
    p.appendChild(this._nbSelect);

    const refreshNbBtn = btn('↻ Refresh', '#64748b', () => this._refreshNotebooks());
    refreshNbBtn.style.marginTop = '-4px';
    p.appendChild(refreshNbBtn);

    p.appendChild(this._label('Container'));
    this._containerSelect = el('select', this._inputCss()) as HTMLSelectElement;
    p.appendChild(this._containerSelect);

    p.appendChild(this._label('Partition'));
    this._partitionSelect = el('select', this._inputCss()) as HTMLSelectElement;
    ['small', 'large', 'gpu', 'gpumedium', 'longrun'].forEach(v => {
      const o = document.createElement('option');
      o.value = o.textContent = v;
      this._partitionSelect.appendChild(o);
    });
    p.appendChild(this._partitionSelect);

    // CPUs slider
    const cpuRow = el('div', 'display:flex;align-items:center;gap:8px;');
    p.appendChild(this._label('CPUs'));
    this._cpuRange = el('input', 'flex:1;') as HTMLInputElement;
    this._cpuRange.type = 'range';
    this._cpuRange.min = '1'; this._cpuRange.max = '40'; this._cpuRange.value = '4';
    this._cpuLabel = el('span', 'font-size:12px;width:24px;text-align:right;', '4') as HTMLSpanElement;
    this._cpuRange.oninput = () => { this._cpuLabel.textContent = this._cpuRange.value; };
    cpuRow.appendChild(this._cpuRange);
    cpuRow.appendChild(this._cpuLabel);
    p.appendChild(cpuRow);

    // Memory slider
    const memRow = el('div', 'display:flex;align-items:center;gap:8px;');
    p.appendChild(this._label('RAM (GB)'));
    this._memRange = el('input', 'flex:1;') as HTMLInputElement;
    this._memRange.type = 'range';
    this._memRange.min = '1'; this._memRange.max = '382'; this._memRange.value = '16';
    this._memLabel = el('span', 'font-size:12px;width:32px;text-align:right;', '16 GB') as HTMLSpanElement;
    this._memRange.oninput = () => { this._memLabel.textContent = `${this._memRange.value} GB`; };
    memRow.appendChild(this._memRange);
    memRow.appendChild(this._memLabel);
    p.appendChild(memRow);

    p.appendChild(this._label('Extra packages (requirements.txt)'));
    this._reqText = el(
      'textarea',
      this._inputCss() + 'height:70px;resize:vertical;font-family:monospace;font-size:11px;'
    ) as HTMLTextAreaElement;
    this._reqText.placeholder = 'numpy\npandas\n...';
    p.appendChild(this._reqText);

    this._submitStatus = el('div', 'font-size:12px;min-height:18px;') as HTMLDivElement;
    const runBtn = btn('▶  Run on Puhti', '#10b981', () => this._submit());
    p.appendChild(runBtn);
    p.appendChild(this._submitStatus);

    this._refreshNotebooks();
    this._loadContainers();
  }

  private async _refreshNotebooks(): Promise<void> {
    const nb = this._tracker.currentWidget;
    const notebooks: string[] = [];
    this._tracker.forEach(w => {
      const path = w.context.path;
      if (path.endsWith('.ipynb') && !path.toLowerCase().includes('launcher')) {
        notebooks.push(path);
      }
    });
    this._nbSelect.innerHTML = '';
    if (notebooks.length === 0) {
      const o = document.createElement('option');
      o.textContent = '(no open notebooks)';
      this._nbSelect.appendChild(o);
    } else {
      notebooks.forEach(p => {
        const o = document.createElement('option');
        o.value = p;
        o.textContent = p.split('/').pop() || p;
        this._nbSelect.appendChild(o);
      });
      // prefer current notebook
      if (nb) {
        this._nbSelect.value = nb.context.path;
      }
    }
  }

  private async _loadContainers(): Promise<void> {
    try {
      const data = await api('GET', '/containers');
      this._containerSelect.innerHTML = '';
      (data.containers as string[]).forEach(c => {
        const o = document.createElement('option');
        o.value = o.textContent = c;
        this._containerSelect.appendChild(o);
      });
    } catch {
      const o = document.createElement('option');
      o.value = 'general-compute';
      o.textContent = 'general-compute';
      this._containerSelect.appendChild(o);
    }
  }

  private async _submit(): Promise<void> {
    const path = this._nbSelect.value;
    if (!path || path.startsWith('(')) {
      this._setStatus(this._submitStatus, 'No notebook selected', 'red');
      return;
    }

    this._setStatus(this._submitStatus, 'Reading notebook…', '#f59e0b');

    // Get notebook content via JupyterLab services
    let nbContent: string;
    try {
      let widget: any = null;
      this._tracker.forEach((w: any) => { if (w.context.path === path) { widget = w; } });
      if (!widget) { throw new Error('Notebook not found — is it open?'); }
      nbContent = JSON.stringify(widget.context.model.toJSON());
    } catch (e) {
      this._setStatus(this._submitStatus, `Could not read notebook: ${e}`, 'red');
      return;
    }

    this._setStatus(this._submitStatus, 'Submitting…', '#f59e0b');
    const fd = new FormData();
    fd.append('notebook', new Blob([nbContent], { type: 'application/json' }), path.split('/').pop() || 'notebook.ipynb');
    fd.append('partition', this._partitionSelect.value);
    fd.append('cpus', this._cpuRange.value);
    fd.append('memory_gb', this._memRange.value);
    fd.append('container', this._containerSelect.value);
    const reqs = this._reqText.value.trim();
    if (reqs) {
      fd.append('requirements', new Blob([reqs], { type: 'text/plain' }), 'requirements.txt');
    }

    try {
      const job = await api('POST', '/run-notebook', fd);
      this._jobId = job.job_id;
      this._slurmId = job.slurm_id;
      this._setStatus(
        this._submitStatus,
        `Submitted — Slurm ${job.slurm_id} — check Jobs tab`,
        '#3b82f6'
      );
    } catch (e) {
      this._setStatus(this._submitStatus, `Submit failed: ${e}`, 'red');
    }
  }

  // ── Jobs tab ────────────────────────────────────────────────────────────────

  private _buildJobs(p: HTMLDivElement): void {
    const row = el('div', 'display:flex;gap:8px;flex-shrink:0;');
    row.appendChild(btn('↻ Refresh', '#3b82f6', () => this._refreshJobs()));
    p.appendChild(row);

    this._statusLabel = el('div', 'font-size:12px;min-height:18px;') as HTMLDivElement;
    p.appendChild(this._statusLabel);

    this._jobsList = el('div', 'display:flex;flex-direction:column;gap:8px;') as HTMLDivElement;
    p.appendChild(this._jobsList);

    this._resultsOut = el('div', 'font-size:12px;color:var(--jp-ui-font-color2);') as HTMLDivElement;
    p.appendChild(this._resultsOut);
  }

  private async _refreshJobs(): Promise<void> {
    if (!this._jobId) {
      this._jobsList.innerHTML = '';
      this._statusLabel.textContent = 'No active job. Submit a notebook first.';
      return;
    }
    try {
      const data = await api('GET', `/run-status/${this._jobId}`);
      const status: string = data.status;
      const color: Record<string, string> = {
        queued: '#f59e0b', running: '#3b82f6', done: '#10b981', failed: '#ef4444'
      };
      this._setStatus(
        this._statusLabel,
        `Slurm ${this._slurmId} — ${status}`,
        color[status] || '#64748b'
      );
      if (status === 'done') {
        await this._fetchResults();
        this._jobId = null;
      } else if (status === 'failed' || status === 'cancelled') {
        await this._showLogs();
        this._jobId = null;
      }
    } catch (e) {
      this._setStatus(this._statusLabel, `Error: ${e}`, 'red');
    }
  }

  private async _fetchResults(): Promise<void> {
    if (!this._jobId && !this._slurmId) { return; }
    const jobId = this._jobId || '';
    try {
      const r = await fetch(`${API}/run-results/${jobId}`);
      if (!r.ok) { throw new Error(await r.text()); }
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `puhti_results_${this._slurmId}.zip`;
      a.click();
      URL.revokeObjectURL(url);
      this._resultsOut.textContent = 'Results downloaded as zip.';
    } catch (e) {
      this._resultsOut.textContent = `Could not fetch results: ${e}`;
    }
  }

  private async _showLogs(): Promise<void> {
    const jobId = this._jobId || '';
    try {
      const data = await api('GET', `/run-logs/${jobId}`);
      this._resultsOut.innerHTML =
        `<b>stdout:</b><pre style="font-size:11px;white-space:pre-wrap;">${data.stdout || '(empty)'}</pre>` +
        `<b>stderr:</b><pre style="font-size:11px;white-space:pre-wrap;">${data.stderr || '(empty)'}</pre>`;
    } catch {
      this._resultsOut.textContent = 'Could not fetch logs.';
    }
  }

  // ── Containers tab ──────────────────────────────────────────────────────────

  private _buildContainers(p: HTMLDivElement): void {
    p.appendChild(btn('↻ Refresh list', '#64748b', () => this._refreshContainers()));

    this._containersList = el('div', 'display:flex;flex-direction:column;gap:4px;') as HTMLDivElement;
    p.appendChild(this._containersList);

    p.appendChild(el('hr', 'border:none;border-top:1px solid var(--jp-border-color2);margin:4px 0;'));
    p.appendChild(this._label('Request new container — upload .def file'));

    // file input (hidden)
    this._defInput = el('input', 'display:none;') as HTMLInputElement;
    this._defInput.type = 'file';
    this._defInput.accept = '.def';
    this._defInput.onchange = () => {
      const f = this._defInput.files?.[0];
      if (!f) { return; }
      this._defFname = f.name;
      this._defFilename.textContent = f.name;
      const reader = new FileReader();
      reader.onload = e => {
        this._defBytes = new Uint8Array(e.target!.result as ArrayBuffer);
      };
      reader.readAsArrayBuffer(f);
    };
    p.appendChild(this._defInput);

    const chooseBtn = btn('📁 Choose .def file', '#64748b', () => this._defInput.click());
    this._defFilename = el('span', 'font-size:11px;color:var(--jp-ui-font-color2);margin-left:8px;', 'No file chosen') as HTMLSpanElement;
    const fileRow = el('div', 'display:flex;align-items:center;flex-wrap:wrap;gap:4px;');
    fileRow.appendChild(chooseBtn);
    fileRow.appendChild(this._defFilename);
    p.appendChild(fileRow);

    p.appendChild(this._label('Description (optional)'));
    this._defDesc = el('input', this._inputCss()) as HTMLInputElement;
    this._defDesc.placeholder = 'e.g. Machine learning container with PyTorch';
    p.appendChild(this._defDesc);

    this._requestStatus = el('div', 'font-size:12px;min-height:18px;') as HTMLDivElement;
    p.appendChild(btn('Request Container', '#f59e0b', () => this._requestContainer()));
    p.appendChild(this._requestStatus);
  }

  private async _refreshContainers(): Promise<void> {
    this._containersList.innerHTML = '';
    try {
      const data = await api('GET', '/containers');
      (data.containers as string[]).forEach(c => {
        const row = el('div', 'font-size:12px;padding:4px 8px;background:var(--jp-layout-color2);border-radius:4px;', `📦 ${c}`);
        this._containersList.appendChild(row);
      });
      // also refresh the submit tab dropdown
      this._loadContainers();
    } catch (e) {
      this._containersList.textContent = `Error: ${e}`;
    }
  }

  private async _requestContainer(): Promise<void> {
    if (!this._defBytes || !this._defFname) {
      this._setStatus(this._requestStatus, 'Choose a .def file first', 'red');
      return;
    }
    this._setStatus(this._requestStatus, 'Opening PR…', '#f59e0b');
    const fd = new FormData();
    fd.append('def_file', new Blob([this._defBytes], { type: 'text/plain' }), this._defFname);
    fd.append('description', this._defDesc.value.trim());
    try {
      const result = await api('POST', '/request-container', fd);
      this._setStatus(
        this._requestStatus,
        `PR opened: ${result.pr_url} — container name: ${result.container_name}`,
        '#10b981'
      );
    } catch (e) {
      this._setStatus(this._requestStatus, `Failed: ${e}`, 'red');
    }
  }

  // ── Utilities ───────────────────────────────────────────────────────────────

  private _label(text: string): HTMLElement {
    return el('div', 'font-size:11px;font-weight:600;color:var(--jp-ui-font-color2);margin-top:4px;text-transform:uppercase;letter-spacing:0.4px;', text);
  }

  private _inputCss(): string {
    return 'width:100%;box-sizing:border-box;border:1px solid var(--jp-border-color2);' +
      'border-radius:5px;padding:5px 8px;font-size:12px;font-family:inherit;' +
      'background:var(--jp-layout-color1);color:var(--jp-ui-font-color1);outline:none;';
  }

  private _setStatus(el: HTMLElement, msg: string, color: string): void {
    el.textContent = msg;
    el.style.color = color;
  }
}

// ── Plugin ───────────────────────────────────────────────────────────────────

const plugin: JupyterFrontEndPlugin<void> = {
  id: 'puhti-runner',
  autoStart: true,
  requires: [ICommandPalette, INotebookTracker as any],
  activate: (
    app: JupyterFrontEnd,
    palette: ICommandPalette,
    tracker: INotebookTracker
  ) => {
    const panel = new PuhtiWidget(tracker);
    app.shell.add(panel, 'right', { rank: 500 });

    const command = 'puhti:toggle';
    app.commands.addCommand(command, {
      label: 'Toggle Puhti Runner',
      execute: () => { app.shell.activateById(panel.id); }
    });
    palette.addItem({ command, category: 'Puhti' });
    app.commands.addKeyBinding({
      command,
      keys: ['Accel Shift P'],
      selector: 'body'
    });
  }
};

export default plugin;
