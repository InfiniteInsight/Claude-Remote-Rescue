/**
 * The window's two tabs. This is a dumb render of the `ControlView` that main
 * pushes: every label and every decision comes from the view-model, and the
 * only thing the page owns is which tab is showing.
 */
import type { ControlView } from '../main/controlView.js';

type Tab = 'control' | 'sessions';

function el<T extends HTMLElement>(id: string): T {
  const node = document.getElementById(id);
  if (!node) throw new Error(`missing #${id}`);
  return node as T;
}

const banner = el<HTMLDivElement>('banner');
const rows = el<HTMLUListElement>('rows');
const startStop = el<HTMLButtonElement>('start-stop');
const repair = el<HTMLButtonElement>('repair');
const refresh = el<HTMLButtonElement>('refresh');
const distroSelect = el<HTMLSelectElement>('distro');
const qrImage = el<HTMLImageElement>('qr-image');
const phoneUrl = el<HTMLParagraphElement>('phone-url');
const phoneUrlText = el<HTMLElement>('phone-url-text');
const phoneText = el<HTMLParagraphElement>('phone-text');
const login = el<HTMLParagraphElement>('login');
const sessionsStopped = el<HTMLDivElement>('sessions-stopped');
const sessionsRunning = el<HTMLParagraphElement>('sessions-running');
const sessionsStart = el<HTMLButtonElement>('sessions-start');

const DOT_TEXT = { on: 'running', off: 'stopped', missing: 'not installed' } as const;
const LOGIN_TEXT = {
  on: 'Start at login: on (change it from the tray menu)',
  off: 'Start at login: off (change it from the tray menu)',
  unknown: 'Start at login: only available in the installed app',
} as const;

let lastView: ControlView | undefined;
let renderedDistros = '';
let renderedDistro: string | undefined;

function show(node: HTMLElement, visible: boolean): void {
  node.classList.toggle('hidden', !visible);
}

/** Never rebuild a control the user may be using, or a poll every 5 s would close an open dropdown. */
function renderDistros(view: ControlView): void {
  const names = [...view.distros];
  if (view.distro !== undefined && !names.includes(view.distro)) names.push(view.distro);
  const key = JSON.stringify(names);
  if (key !== renderedDistros) {
    renderedDistros = key;
    distroSelect.replaceChildren(
      ...names.map((name) => {
        const option = document.createElement('option');
        option.value = name;
        option.textContent = name;
        return option;
      }),
    );
    renderedDistro = undefined;
  }
  if (view.distro !== renderedDistro) {
    renderedDistro = view.distro;
    distroSelect.value = view.distro ?? '';
  }
  // `repair.enabled` is simply "no action is running": a distro switch mid-action would race it.
  distroSelect.disabled = !view.repair.enabled;
}

function render(view: ControlView): void {
  lastView = view;

  banner.textContent = view.banner ?? '';
  show(banner, view.banner !== undefined);

  rows.replaceChildren(
    ...view.rows.map((row) => {
      const li = document.createElement('li');
      const dot = document.createElement('span');
      dot.className = `dot ${row.dot}`;
      const label = document.createElement('span');
      label.textContent = row.label;
      const state = document.createElement('span');
      state.className = 'row-state';
      state.textContent = DOT_TEXT[row.dot];
      li.append(dot, label, state);
      return li;
    }),
  );

  startStop.textContent = view.startStop.label;
  startStop.disabled = !view.startStop.enabled;
  show(repair, view.repair.visible);
  repair.disabled = !view.repair.enabled;

  renderDistros(view);

  const phone = view.phone;
  if (phone.kind === 'qr') {
    if (qrImage.getAttribute('src') !== phone.image) qrImage.setAttribute('src', phone.image);
    phoneUrlText.textContent = phone.url;
    phoneText.textContent = '';
  } else {
    qrImage.removeAttribute('src');
    phoneUrlText.textContent = '';
    phoneText.textContent = phone.text;
  }
  show(qrImage, phone.kind === 'qr');
  show(phoneUrl, phone.kind === 'qr');
  show(phoneText, phone.kind !== 'qr');

  login.textContent = LOGIN_TEXT[view.login];

  // While the dashboard is up, main draws crr's own page over this panel (a
  // WebContentsView below the tab bar); this is what shows when it is not.
  const dashboardOn = view.rows.some((r) => r.name === 'dashboard' && r.dot === 'on');
  show(sessionsStopped, !dashboardOn);
  show(sessionsRunning, dashboardOn);
  sessionsStart.disabled = !view.startStop.enabled;
}

function selectTab(tab: Tab): void {
  for (const button of document.querySelectorAll<HTMLButtonElement>('.tab-button')) {
    const active = button.dataset['tab'] === tab;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
  }
  el('panel-control').classList.toggle('active', tab === 'control');
  el('panel-sessions').classList.toggle('active', tab === 'sessions');
  void window.crr.showTab(tab);
}

for (const button of document.querySelectorAll<HTMLButtonElement>('.tab-button')) {
  button.addEventListener('click', () => selectTab(button.dataset['tab'] as Tab));
}

/** Start or Stop, whichever the view-model currently offers. */
function toggle(): void {
  if (lastView === undefined) return;
  void (lastView.startStop.label.startsWith('Stop') ? window.crr.stop() : window.crr.start());
}

startStop.addEventListener('click', toggle);
sessionsStart.addEventListener('click', () => void window.crr.start());
repair.addEventListener('click', () => void window.crr.repair());
refresh.addEventListener('click', () => void window.crr.refresh());
distroSelect.addEventListener('change', () => void window.crr.setDistro(distroSelect.value));

window.crr.onView(render);
