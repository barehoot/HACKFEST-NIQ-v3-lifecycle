const root = document.getElementById("devplan-root");
const subtitle = document.getElementById("match-subtitle");

const STATUS_INFO = {
  plan_ready: { kind: "info", label: "Plan ready -- every confirmed gap has a verified internal course." },
  plan_ready_with_unaddressed_gaps: {
    kind: "warn",
    label: "Partial plan -- some gaps have no course in our internal catalog yet (see the Coursera leads below).",
  },
  plan_needs_review: { kind: "warn", label: "Plan needs human review -- automatic verification did not fully pass." },
  assessment_pending: { kind: "warn", label: "Some skills haven't been assessed yet -- nothing to plan for until they are." },
  no_gaps_at_target: { kind: "info", label: "Every assessed skill is already at target -- no development plan needed." },
  gaps_unavailable: { kind: "error", label: "No gap data found for this person." },
};

function renderDurationCard(duration) {
  if (!duration) return "";

  const rows = (duration.per_gap || [])
    .map((g) => `<li>${esc(g.skill)}: ~${g.estimated_weeks} week(s)</li>`)
    .join("");

  if (!duration.per_gap || !duration.per_gap.length) {
    return `
      <div class="chart-card">
        <h3>Estimated time to close these gaps</h3>
        <p class="chart-subtitle">${esc(duration.note || "")}</p>
      </div>`;
  }

  return `
    <div class="chart-card">
      <h3>Estimated time to close these gaps</h3>
      <p class="chart-subtitle">${esc(duration.note || "")}</p>
      <div class="hero-figure">${duration.estimated_weeks_one_at_a_time}<span style="font-size:16px;font-weight:600"> weeks</span></div>
      <p class="meta">One course at a time. About ${duration.estimated_weeks_with_two_in_parallel} weeks if you can work two courses in parallel.</p>
      ${duration.multiplier_basis ? `<p class="meta">Adjusted using: ${esc(duration.multiplier_basis)}</p>` : ""}
      <ul style="margin-top:10px">${rows}</ul>
    </div>`;
}

function renderStep(s) {
  return `
    <div class="question-card">
      <div class="q-header">
        <span class="skill-name">${esc(s.step_number)}. ${esc(s.skill)}</span>
        <span class="badge pass">Verified internal course</span>
      </div>
      <div class="q-text"><strong>${esc(s.course_title || s.course_id)}</strong>${s.delivery_type ? ` &middot; ${esc(s.delivery_type)}` : ""}</div>
      <div class="meta">Closes: ${esc(s.descriptor_clause_addressed)}</div>
      ${s.rationale ? `<div class="meta" style="margin-top:4px">${esc(s.rationale)}</div>` : ""}
    </div>`;
}

function renderUnaddressed(gaps) {
  if (!gaps || !gaps.length) return "";
  return `
    <div class="skipped-box">
      <strong>${gaps.length} gap(s) with no internal course yet</strong> -- see the Coursera leads below for these:
      <ul>${gaps
        .map(
          (g) =>
            `<li><strong>${esc(g.skill)}</strong> (${esc(g.gap_severity)}${g.is_critical ? ", critical" : ""}) -- ${esc(g.reason)}</li>`
        )
        .join("")}</ul>
    </div>`;
}

function renderCoursera(suggestions) {
  if (!suggestions || !suggestions.length) return "";

  const cards = suggestions
    .map((s) => {
      const courseHtml =
        s.courses && s.courses.length
          ? s.courses
              .map(
                (c) => `
            <div style="margin-bottom:10px">
              <a href="${c.search_url}" target="_blank" rel="noopener">${esc(c.title)}</a>
              <span class="badge pending">AI-suggested, unverified</span>
              <div class="meta">${esc(c.why)}</div>
            </div>`
              )
              .join("")
          : `<div class="meta">No specific title suggested -- <a href="${s.skill_search_url}" target="_blank" rel="noopener">search Coursera for "${esc(
              s.skill
            )}"</a> directly.</div>`;

      return `
        <div class="question-card">
          <div class="q-header">
            <span class="skill-name">${esc(s.skill)}${s.is_critical ? " (critical)" : ""}</span>
            ${s.gap_severity ? `<span class="badge fail">${esc(s.gap_severity)}</span>` : ""}
          </div>
          ${courseHtml}
        </div>`;
    })
    .join("");

  return `
    <div class="chart-card">
      <h3>Probable courses from Coursera</h3>
      <p class="chart-subtitle">Named by the model from its own general knowledge -- not checked against Coursera's live catalog. Every link goes to a real Coursera search for the topic, whether or not the exact title is still listed.</p>
      ${cards}
    </div>`;
}

function render() {
  const raw = sessionStorage.getItem("talent360i_devplan");

  if (!raw) {
    root.innerHTML = `<div class="status-box warn">No development plan found -- go back to your evidence report and click "View Development Plan".</div>`;
    return;
  }

  const { match, result } = JSON.parse(raw);

  if (match) {
    subtitle.innerHTML = `${esc(result.role_name || match.role_name)} ${frameworkBadge(match.framework)} &middot; ${esc(result.role_id || match.role_id)}`;
  }

  const info = STATUS_INFO[result.status] || { kind: "warn", label: result.status };

  let html = `<div class="status-box ${info.kind}">${esc(info.label)}</div>`;

  if (result.status === "gaps_unavailable") {
    root.innerHTML = html;
    return;
  }

  html += renderDurationCard(result.duration_estimate);

  if (result.steps && result.steps.length) {
    html += result.steps.map(renderStep).join("");
  }

  html += renderUnaddressed(result.unaddressed_gaps);
  html += renderCoursera(result.coursera_suggestions);

  root.innerHTML = html;
}

render();
