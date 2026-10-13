// Shared chart helpers used by results.js and evidence.js -- factored out
// once a second page needed the same meter/badge components, per the
// dataviz method (one hue, severity-colored meter; status colors reused
// from the app's existing tokens rather than a competing palette).

function esc(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

function frameworkBadge(framework) {
  const cls = framework === "RD" ? "rd" : "finops";
  return `<span class="badge ${cls}">${esc(framework)}</span>`;
}

function severityColor(pct) {
  if (pct == null) return "var(--status-neutral)";
  if (pct >= 75) return "var(--status-good)";
  if (pct >= 45) return "var(--amber)";
  return "var(--status-critical)";
}

// A single ratio-against-a-limit card: hero figure + meter track/fill,
// severity-colored. subtitle is optional context text.
function renderMeterCard(title, subtitle, pct) {
  const pctText = pct == null ? "n/a" : `${pct}%`;
  const color = severityColor(pct);
  const width = pct == null ? 0 : pct;

  return `
    <div class="chart-card">
      <h3>${esc(title)}</h3>
      ${subtitle ? `<p class="chart-subtitle">${subtitle}</p>` : ""}
      <div class="hero-figure" style="color:${color}">${pctText}</div>
      <div class="meter-track">
        <div class="meter-fill" style="width:${width}%; background:${color}"></div>
      </div>
    </div>`;
}
