document.querySelectorAll(".filters select").forEach((select) => {
  select.addEventListener("change", () => select.form.requestSubmit());
});
let submittingCommand = false;
document.addEventListener('input', (event) => {
  if (event.target.closest('.schedule-form')) event.target.closest('.live-processing').dataset.editing = 'true';
});
document.addEventListener("submit", (event) => {
  const form = event.target;
  if (form.closest('.connection-panel')) {
    submittingCommand = true;
    const button = form.querySelector('button');
    button.disabled = true;
    button.textContent = 'Проверяем подключение…';
    return;
  }
  if (!form.closest(".admin-panel, .task-controls, .schedule-form, .article-retry, .notification-form")) return;
  submittingCommand = true;
  const button = form.querySelector("button");
  button.disabled = true;
  if (button.classList.contains("icon-button")) {
    button.setAttribute("aria-label", "Сохраняем команду…");
  } else {
    button.textContent = form.closest(".notification-form") ? "Отмечаем прочитанными…" : form.closest(".task-controls") ? "Сохраняем…" : "Запускаем…";
  }
});
document.querySelectorAll('[data-section-nav]').forEach((select) => {
  select.addEventListener('change', () => location.assign(select.value));
});
const healthPanel = document.getElementById('system-health');
const adminSystemPanel = document.getElementById('admin-system-status');
if (adminSystemPanel) {
  let refreshing = false;
  setInterval(async () => {
    if (refreshing || submittingCommand || document.hidden || adminSystemPanel.contains(document.activeElement)) return;
    refreshing = true;
    try {
      const response = await fetch(adminSystemPanel.dataset.refreshUrl, {cache: 'no-store'});
      if (response.ok) adminSystemPanel.innerHTML = await response.text();
    } catch { /* Preserve the last confirmed state. */ }
    finally { refreshing = false; }
  }, 5000);
}
if (healthPanel) {
  let refreshing = false;
  setInterval(async () => {
    if (refreshing || submittingCommand || document.hidden || healthPanel.contains(document.activeElement) || healthPanel.querySelector('details[open]')) return;
    refreshing = true;
    try {
      const response = await fetch(healthPanel.dataset.refreshUrl, {cache: 'no-store'});
      if (response.ok) healthPanel.innerHTML = await response.text();
    } catch { /* Preserve the last confirmed service state. */ }
    finally { refreshing = false; }
  }, 10000);
}
document.addEventListener("click", (event) => {
  document.querySelectorAll(".export-menu[open]").forEach((menu) => {
    if (!menu.contains(event.target)) menu.open = false;
  });
  document.querySelectorAll(".profile-menu[open]").forEach((menu) => { if (!menu.contains(event.target)) menu.open = false; });
  document.querySelectorAll(".help-disclosure[open]").forEach((help) => {
    if (!help.contains(event.target)) help.open = false;
  });
});
const overview = document.getElementById("task-overview");
const countdownClocks = new WeakMap();
function updateCountdowns() {
  document.querySelectorAll("[data-countdown]").forEach((element) => {
    if (!countdownClocks.has(element)) {
      countdownClocks.set(element, {at: performance.now(), server: Number(element.dataset.serverNow)});
    }
    const clock = countdownClocks.get(element);
    const remaining = Number(element.dataset.countdown) - clock.server - (performance.now() - clock.at) / 1000;
    if (!Number.isFinite(remaining)) return;
    if (remaining <= 0) {
      element.textContent = "Запуск ожидается";
      return;
    }
    if (remaining < 60) {
      element.textContent = "менее минуты";
      return;
    }
    const minutes = Math.floor(remaining / 60);
    const parts = [Math.floor(minutes / 1440), Math.floor(minutes / 60) % 24, minutes % 60];
    element.textContent = "через " + parts.map((part, index) => part ? `${part} ${["д", "ч", "мин"][index]}` : "").filter(Boolean).join(" ");
  });
}
updateCountdowns();
setInterval(updateCountdowns, 10000);
function revealFragment() {
  const id = location.hash.slice(1);
  const target = id && document.getElementById(id);
  if (!target) return;
  if (target.tagName === "DETAILS") target.open = true;
  if (id === "reports" || id === "history") target.focus({preventScroll: true});
  target.scrollIntoView({block: "start"});
}
revealFragment();
window.addEventListener("hashchange", revealFragment);
document.querySelectorAll('.live-processing').forEach((panel) => {
  let refreshing = false;
  setInterval(async () => {
    if (refreshing || submittingCommand || document.hidden || panel.dataset.editing || panel.contains(document.activeElement) || panel.querySelector('details[open]')) return;
    refreshing = true;
    try {
      const response = await fetch(panel.dataset.refreshUrl, {cache: 'no-store'});
      if (response.ok) {
        panel.innerHTML = await response.text();
        updateCountdowns();
      }
    } catch { /* Keep the last confirmed status until connectivity returns. */ }
    finally { refreshing = false; }
  }, 3000);
});
function followOutput(panel, previous) {
  const output = panel.querySelector('.console-output');
  if (!output) return;
  output.scrollTop = previous && !previous.bottom ? previous.top : output.scrollHeight;
}
function liveJournal(panel, follow, refreshButton) {
  if (!panel || !follow) return;
  let refreshing = false;
  let finished = panel.querySelector('[data-finished="true"]') !== null;
  const refresh = async (force = false) => {
    if (refreshing || !force && (finished || !follow.checked || document.hidden || window.getSelection()?.toString() || panel.querySelector('details[open]'))) return;
    refreshing = true;
    const old = panel.querySelector('.console-output');
    const position = old && {top: old.scrollTop, bottom: old.scrollHeight - old.scrollTop - old.clientHeight < 60};
    try {
      const response = await fetch(panel.dataset.feedUrl, {cache: 'no-store'});
      if (response.ok) {
        panel.innerHTML = await response.text();
        finished = panel.querySelector('[data-finished="true"]') !== null;
        if (follow.checked) followOutput(panel, position);
      }
    } catch { /* Preserve the last confirmed journal. */ }
    finally { refreshing = false; }
  };
  setInterval(() => refresh(), 2000);
  follow.addEventListener('change', () => refresh(true));
  refreshButton?.addEventListener('click', () => refresh(true));
  followOutput(panel);
}
liveJournal(document.getElementById('work-console'), document.getElementById('console-follow'), document.getElementById('console-refresh'));
liveJournal(document.getElementById('live-run'), document.getElementById('run-follow'));
const liveLaunch = document.getElementById('live-launch');
if (liveLaunch) {
  let refreshing = false;
  setInterval(async () => {
    if (refreshing || document.hidden) return;
    refreshing = true;
    try {
      const response = await fetch(liveLaunch.dataset.feedUrl, {cache: 'no-store'});
      if (response.ok) {
        liveLaunch.innerHTML = await response.text();
        const destination = liveLaunch.querySelector('[data-run-url]')?.dataset.runUrl;
        if (destination) location.replace(destination);
      }
    } catch { /* Keep the request page during a temporary disconnect. */ }
    finally { refreshing = false; }
  }, 1000);
}
document.querySelectorAll('.activity-filters select, .activity-filters input[type="date"]').forEach((field) => {
  field.addEventListener('change', () => field.form.requestSubmit());
});
document.querySelectorAll('.live-history').forEach((panel) => {
  let refreshing = false;
  setInterval(async () => {
    if (refreshing || document.hidden || panel.contains(document.activeElement) || window.getSelection()?.toString()) return;
    refreshing = true;
    try {
      const response = await fetch(panel.dataset.refreshUrl, {cache: 'no-store'});
      if (response.ok) panel.innerHTML = await response.text();
    } catch { /* Keep the last confirmed history. */ }
    finally { refreshing = false; }
  }, 15000);
});
if (overview) {
  let refreshing = false;
  setInterval(async () => {
    if (
      refreshing ||
      submittingCommand ||
      document.hidden ||
      overview.contains(document.activeElement) ||
      overview.querySelector("details[open]")
    )
      return;
    refreshing = true;
    try {
      const response = await fetch(overview.dataset.refreshUrl, {
        cache: "no-store",
      });
      if (response.ok) {
        overview.innerHTML = await response.text();
        updateCountdowns();
      }
    } catch {
      // Keep the last visible state until the next successful refresh.
    } finally {
      refreshing = false;
    }
  }, 15000);
}

document.addEventListener('toggle', async (event) => {
  const details = event.target;
  if (!details.matches?.('details[data-event-url]') || !details.open || details.dataset.loaded || details.dataset.loading) return;
  details.dataset.loading = 'true';
  try {
    const response = await fetch(details.dataset.eventUrl, {cache: 'no-store'});
    if (response.ok) {
      details.querySelector('pre').textContent = await response.text();
      details.dataset.loaded = 'true';
    } else details.querySelector('pre').textContent = 'Не удалось открыть подробности. Обновите страницу.';
  } catch { details.querySelector('pre').textContent = 'Нет связи. Закройте и откройте подробности для повтора.'; }
  finally { delete details.dataset.loading; }
}, true);
