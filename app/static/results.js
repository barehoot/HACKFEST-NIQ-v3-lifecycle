const root = document.getElementById("analytics-root");
const subtitle = document.getElementById("match-subtitle");

function renderHero(score) {
  return renderMeterCard(
    "Overall score",
    "Weighted by each skill's target proficiency level, doubled for critical skills.",
    score.overall_score_pct
  );
}

// Part-to-whole breakdown: one 100%-wide stacked bar, 2px surface gaps
// between segments, legend always present (>=2 series) carrying the
// counts a narrow segment has no room to direct-label.
function renderBreakdown(score, totalItems) {
  const correct = score.results.filter((r) => r.is_correct).length;
  const incorrect = score.results.filter((r) => !r.is_correct).length;
  const unanswered = score.unanswered.length;
  const total = Math.max(totalItems, correct + incorrect + unanswered, 1);

  const segments = [
    { label: "Correct", count: correct, color: "var(--status-good)" },
    { label: "Incorrect", count: incorrect, color: "var(--status-critical)" },
    { label: "Unanswered", count: unanswered, color: "var(--status-neutral)" },
  ].filter((s) => s.count > 0);

  const barHtml = segments
    .map((s) => `<div class="stacked-segment" style="width:${(100 * s.count) / total}%; background:${s.color}" title="${esc(s.label)}: ${s.count}"></div>`)
    .join("");

  const legendHtml = segments
    .map(
      (s) => `<div class="legend-item">
        <span class="legend-swatch" style="background:${s.color}"></span>
        ${esc(s.label)}: ${s.count}
      </div>`
    )
    .join("");

  return `
    <div class="chart-card">
      <h3>Answer breakdown</h3>
      <p class="chart-subtitle">${totalItems} question(s) total.</p>
      <div class="stacked-bar">${barHtml}</div>
      <div class="chart-legend">${legendHtml}</div>
    </div>`;
}

// Per-skill magnitude comparison: single series (percent correct), one
// consistent hue, direct-labeled at the bar tip. Criticality is a
// secondary text-token cue (a dot beside the label), never a second hue
// on the bar itself.
function renderSkillBars(score) {
  if (!score.by_skill.length) {
    return `
      <div class="chart-card">
        <h3>By skill</h3>
        <p class="chart-subtitle">No answered questions to break down by skill.</p>
      </div>`;
  }

  const rows = score.by_skill
    .map(
      (s) => `
      <div class="skill-bar-row">
        <div class="skill-bar-label" title="${esc(s.skill)}">
          ${s.is_critical ? `<span class="critical-dot" title="Critical skill"></span>` : ""}${esc(s.skill)}
        </div>
        <div class="skill-bar-track" title="${esc(s.skill)}: ${s.correct}/${s.total} correct">
          <div class="skill-bar-fill" style="width:${s.pct}%"></div>
        </div>
        <div class="skill-bar-value">${s.correct}/${s.total}</div>
      </div>`
    )
    .join("");

  const tableRows = score.by_skill
    .map(
      (s) => `<tr>
        <td>${esc(s.skill)}${s.is_critical ? " (critical)" : ""}</td>
        <td>${s.target_proficiency_level}</td>
        <td>${s.correct}/${s.total}</td>
        <td>${s.pct}%</td>
      </tr>`
    )
    .join("");

  return `
    <div class="chart-card">
      <h3>By skill</h3>
      <p class="chart-subtitle">Percent correct per skill. The dot marks critical skills.</p>
      ${rows}
      <button type="button" class="table-toggle" id="table-toggle">Show as table</button>
      <table class="analytics-table" id="skill-table" hidden>
        <thead><tr><th>Skill</th><th>Target level</th><th>Correct</th><th>%</th></tr></thead>
        <tbody>${tableRows}</tbody>
      </table>
    </div>`;
}

let currentMatch = null;
let currentScore = null;

async function goToCaseStudyRound() {
  const btn = document.getElementById("case-study-btn");
  const statusEl = document.getElementById("case-study-status");
  if (btn) btn.disabled = true;
  if (statusEl) statusEl.innerHTML = `<span class="spinner"></span>Generating case-study questions&hellip;`;

  try {
    const res = await fetch("/api/case-studies", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role_id: currentMatch.role_id, score: currentScore, max_questions: 3 }),
    });
    const data = await res.json();

    if (!res.ok) {
      if (statusEl) statusEl.innerHTML = `<div class="status-box error">${esc(data.message || "Could not generate case studies.")}</div>`;
      if (btn) btn.disabled = false;
      return;
    }

    sessionStorage.setItem(
      "talent360i_case_study",
      JSON.stringify({ match: currentMatch, score: currentScore, ...data })
    );
    window.location.href = "/case-study";
  } catch (err) {
    if (statusEl) statusEl.innerHTML = `<div class="status-box error">Request failed: ${esc(err.message)}</div>`;
    if (btn) btn.disabled = false;
  }
}

function render() {
  const raw = sessionStorage.getItem("talent360i_results");

  if (!raw) {
    root.innerHTML = `<div class="status-box warn">No results found -- take an assessment from the dashboard first.</div>`;
    return;
  }

  const { match, items, score } = JSON.parse(raw);
  currentMatch = match;
  currentScore = score;

  if (match) {
    subtitle.innerHTML = `${esc(match.role_name)} ${frameworkBadge(match.framework)} &middot; ${esc(match.role_id)}`;
  }

  root.innerHTML =
    renderHero(score) +
    renderBreakdown(score, items.length) +
    renderSkillBars(score) +
    `<div class="chart-card">
       <h3>Case-study round</h3>
       <p class="chart-subtitle">Agent 4 reads this report and writes open-ended case-study questions for your weakest / most critical skills, then evaluates your written answers as evidence.</p>
       <button type="button" id="case-study-btn" class="answer-submit">Continue to Case-Study Round</button>
       <div id="case-study-status" style="margin-top:10px"></div>
     </div>`;

  const toggle = document.getElementById("table-toggle");
  const table = document.getElementById("skill-table");
  if (toggle && table) {
    toggle.addEventListener("click", () => {
      const showing = !table.hidden;
      table.hidden = showing;
      toggle.textContent = showing ? "Show as table" : "Hide table";
    });
  }

  const csBtn = document.getElementById("case-study-btn");
  if (csBtn) csBtn.addEventListener("click", goToCaseStudyRound);
}

render();
