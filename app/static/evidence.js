const root = document.getElementById("evidence-root");
const subtitle = document.getElementById("match-subtitle");

const VERDICT_BADGE = {
  strong_evidence: "pass",
  partial_evidence: "pending",
  insufficient_evidence: "pending",
  no_evidence: "fail",
};

const VERDICT_LABEL = {
  strong_evidence: "Strong evidence",
  partial_evidence: "Partial evidence",
  insufficient_evidence: "Insufficient evidence",
  no_evidence: "No evidence",
};

function renderEvidenceCard(r) {
  const quotesHtml = r.quotes && r.quotes.length
    ? r.quotes.map((q) => `<div class="quote">&ldquo;${esc(q)}&rdquo;</div>`).join("")
    : `<div class="quote">No quotable evidence found.</div>`;

  const missingHtml = r.missing_element
    ? `<div class="missing-note">Missing: ${esc(r.missing_element)}</div>`
    : "";

  return `
    <div class="question-card">
      <div class="q-header">
        <span class="skill-name">${esc(r.skill)} (Level ${r.target_proficiency_level}${r.is_critical ? ", critical" : ""})</span>
        <span class="badge ${VERDICT_BADGE[r.verdict] || "pending"}">${esc(VERDICT_LABEL[r.verdict] || r.verdict)}</span>
      </div>
      ${quotesHtml}
      ${missingHtml}
      <div class="meta" style="margin-top:8px">${esc(r.reasoning)}</div>
    </div>`;
}

let state = null; // { match, score, report }

async function goToDevPlan() {
  const btn = document.getElementById("devplan-btn");
  const statusEl = document.getElementById("devplan-status");
  if (btn) btn.disabled = true;
  if (statusEl) statusEl.innerHTML = `<span class="spinner"></span>Building your development plan&hellip; this can take 15&ndash;30s.`;

  try {
    const res = await fetch("/api/devplan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        role_id: state.report.role_id,
        score: state.score,
        evidence: state.report,
      }),
    });
    const data = await res.json();

    if (!res.ok) {
      if (statusEl) statusEl.innerHTML = `<div class="status-box error">${esc(data.message || "Could not build a development plan.")}</div>`;
      if (btn) btn.disabled = false;
      return;
    }

    sessionStorage.setItem("talent360i_devplan", JSON.stringify({ match: state.match, result: data }));
    window.location.href = "/devplan";
  } catch (err) {
    if (statusEl) statusEl.innerHTML = `<div class="status-box error">Request failed: ${esc(err.message)}</div>`;
    if (btn) btn.disabled = false;
  }
}

function render() {
  const raw = sessionStorage.getItem("talent360i_evidence");

  if (!raw) {
    root.innerHTML = `<div class="status-box warn">No evidence report found -- complete a case-study round from your results page first.</div>`;
    return;
  }

  state = JSON.parse(raw);
  const { match, report } = state;

  if (match) {
    subtitle.innerHTML = `${esc(report.role_name)} ${frameworkBadge(match.framework)} &middot; ${esc(report.role_id)} (Grade ${esc(report.role_grade)})`;
  }

  const bandText = report.compatibility_band
    ? `${esc(report.compatibility_band)} -- <span class="badge pending">${esc(report.manager_review_status)}</span>`
    : `<span class="badge pending">${esc(report.manager_review_status)}</span>`;

  root.innerHTML =
    renderMeterCard("Compatibility for this role", bandText, report.compatibility_score_pct) +
    `<div class="chart-row">
       ${renderMeterCard("MCQ score (Agent 3)", "Arithmetic, from the multiple-choice round.", report.mcq_score_pct)}
       ${renderMeterCard("Evidence score (Agent 4)", "From written-answer evidence, this round.", report.evidence_score_pct)}
     </div>` +
    `<div class="status-box warn" style="margin-top:16px">This is Agent 4's evidence-based proposal, not a decision -- a human manager still needs to review and confirm it.</div>` +
    report.evidence_results.map(renderEvidenceCard).join("") +
    `<div class="chart-card">
       <h3>Development plan</h3>
       <p class="chart-subtitle">Agent 5 turns these gaps into a sequenced plan, probable Coursera courses, and a rough timeline to close them.</p>
       <button type="button" id="devplan-btn" class="answer-submit">View Development Plan</button>
       <div id="devplan-status" style="margin-top:10px"></div>
     </div>`;

  const btn = document.getElementById("devplan-btn");
  if (btn) btn.addEventListener("click", goToDevPlan);
}

render();
