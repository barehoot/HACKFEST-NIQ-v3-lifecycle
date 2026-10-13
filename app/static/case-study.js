const root = document.getElementById("case-study-root");
const subtitle = document.getElementById("match-subtitle");

let state = null; // { match, score, role_id, role_name, case_studies, skills_skipped }

function renderSkipped(skipped) {
  if (!skipped || !skipped.length) return "";
  return `
    <div class="skipped-box">
      <strong>${skipped.length} skill(s) skipped</strong> -- no case study was written for these:
      <ul>${skipped.map((s) => `<li><strong>${esc(s.skill)}</strong> -- ${esc(s.reason)}</li>`).join("")}</ul>
    </div>`;
}

function renderCaseStudyCard(cs, index) {
  return `
    <div class="question-card">
      <div class="q-header">
        <span class="skill-name">${index + 1}. ${esc(cs.skill)} (Level ${cs.target_proficiency_level}${cs.is_critical ? ", critical" : ""})</span>
        ${cs.is_synthetic_grounding ? `<span class="badge pending" title="Grounded on placeholder data, not real SME-authored content">Synthetic grounding</span>` : ""}
      </div>
      <div class="q-text">${esc(cs.question_text)}</div>
      <textarea class="answer-textarea" id="answer-${esc(cs.question_id)}" rows="6" placeholder="Describe what you did, why, and what happened -- specific detail is what Agent 4 looks for."></textarea>
    </div>`;
}

async function submitEvidence() {
  const answers = {};
  for (const cs of state.case_studies) {
    const el = document.getElementById(`answer-${cs.question_id}`);
    answers[cs.question_id] = el ? el.value : "";
  }

  const btn = document.getElementById("evidence-submit-btn");
  const statusEl = document.getElementById("evidence-status");
  if (btn) btn.disabled = true;
  if (statusEl) statusEl.innerHTML = `<span class="spinner"></span>Analyzing your answers for evidence&hellip; this can take 15&ndash;30s.`;

  try {
    const res = await fetch("/api/evidence", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        case_studies: state.case_studies,
        answers,
        score: state.score,
        role_id: state.match.role_id,
        role_name: state.match.role_name,
        role_grade: state.match.role_grade,
      }),
    });
    const data = await res.json();

    if (!res.ok) {
      if (statusEl) statusEl.innerHTML = `<div class="status-box error">${esc(data.message || "Evidence analysis failed.")}</div>`;
      if (btn) btn.disabled = false;
      return;
    }

    sessionStorage.setItem(
      "talent360i_evidence",
      JSON.stringify({ match: state.match, score: state.score, report: data })
    );
    window.location.href = "/evidence";
  } catch (err) {
    if (statusEl) statusEl.innerHTML = `<div class="status-box error">Request failed: ${esc(err.message)}</div>`;
    if (btn) btn.disabled = false;
  }
}

function render() {
  const raw = sessionStorage.getItem("talent360i_case_study");

  if (!raw) {
    root.innerHTML = `<div class="status-box warn">No case-study session found -- go back to your results and click "Continue to Case-Study Round".</div>`;
    return;
  }

  state = JSON.parse(raw);

  if (state.match) {
    subtitle.innerHTML = `${esc(state.match.role_name)} ${frameworkBadge(state.match.framework)} &middot; ${esc(state.match.role_id)}`;
  }

  if (!state.case_studies || !state.case_studies.length) {
    root.innerHTML =
      `<div class="status-box warn">No case-study questions could be generated -- every candidate skill was missing the grounding data needed.</div>` +
      renderSkipped(state.skills_skipped);
    return;
  }

  root.innerHTML =
    state.case_studies.map(renderCaseStudyCard).join("") +
    renderSkipped(state.skills_skipped) +
    `<button type="button" id="evidence-submit-btn" class="answer-submit">Submit Evidence</button>
     <div id="evidence-status" style="margin-top:10px"></div>`;

  const btn = document.getElementById("evidence-submit-btn");
  if (btn) btn.addEventListener("click", submitEvidence);
}

render();
