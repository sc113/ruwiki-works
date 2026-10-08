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
    button.textContent = form.closest(".task-controls") ? "Сохраняем…" : "Добавляем в очередь…";
  }
});
document.querySelectorAll('[data-section-nav]').forEach((select) => {
  select.addEventListener('change', () => location.assign(select.value));
});
const healthPanel = document.getElementById('system-health');
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
const workConsole = document.getElementById('work-console');
if (workConsole) {
  const follow = document.getElementById('console-follow');
  let requestNumber = 0;
  const refresh = async (force = false) => {
    if (!force && (!follow.checked || document.hidden || window.getSelection()?.toString() || workConsole.querySelector('details[open]') || workConsole.contains(document.activeElement))) return;
    const number = ++requestNumber;
    try {
      const response = await fetch(workConsole.dataset.feedUrl, {cache: 'no-store'});
      if (response.ok && number === requestNumber) {
        workConsole.innerHTML = await response.text();
        if (follow.checked) {
          const output = workConsole.querySelector('.console-output');
          output.scrollTop = output.scrollHeight;
        }
      }
    } catch { /* Preserve the current console contents. */ }
  };
  setInterval(() => refresh(), 3000);
  follow.addEventListener('change', () => refresh(true));
  document.getElementById('console-refresh').addEventListener('click', () => refresh(true));
  const output = workConsole.querySelector('.console-output');
  output.scrollTop = output.scrollHeight;
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
