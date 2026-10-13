const form = document.getElementById("upload-form");
const fileInput = document.getElementById("file-input");
const dropzone = document.getElementById("dropzone");
const dropzoneLabel = document.getElementById("dropzone-label");
const statusArea = document.getElementById("status-area");
const resultsArea = document.getElementById("results");
const submitBtn = document.getElementById("submit-btn");

function esc(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

fileInput.addEventListener("change", () => {
  if (fileInput.files[0]) {
    dropzoneLabel.textContent = fileInput.files[0].name;
  }
});

["dragover", "dragleave", "drop"].forEach((evt) => {
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.toggle("dragover", evt === "dragover");
  });
});

dropzone.addEventListener("drop", (e) => {
  const dropped = e.dataTransfer.files;
  if (dropped.length) {
    fileInput.files = dropped;
    dropzoneLabel.textContent = dropped[0].name;
  }
});

function setStatus(kind, html) {
  statusArea.innerHTML = `<div class="status-box ${kind}">${html}</div>`;
}

function clearStatus() {
  statusArea.innerHTML = "";
}

function frameworkBadge(framework) {
  const cls = framework === "RD" ? "rd" : "finops";
  return `<span class="badge ${cls}">${esc(framework)}</span>`;
}

function renderMatch(match, counts) {
  const countsHtml = counts
    ? `<div class="counts">
         <span class="badge pass">${counts.approved} approved</span>
         ${counts.failed ? `<span class="badge fail">${counts.failed} failed self-check</span>` : ""}
         ${counts.skipped ? `<span class="badge pending">${counts.skipped} skipped</span>` : ""}
       </div>`
    : "";

  return `
    <div class="match-card">
      <div>
        <h2>${esc(match.role_name)} ${frameworkBadge(match.framework)}</h2>
        <div class="meta">${esc(match.role_id)} &middot; Grade ${esc(match.role_grade)} &middot; Tower ${esc(match.tower)} &middot; ${match.skill_count} skills in blueprint</div>
      </div>
      ${countsHtml}
    </div>`;
}

// Unanswered question form -- deliberately never shows correct_option or
// self_check notes here, since self-check notes can reference the
// correct answer (e.g. "the correct answer is justified because...")
// and would spoil the question before the candidate answers it.
function renderQuestionForm(item, index) {
  const options = [
    ["A", item.option_a],
    ["B", item.option_b],
    ["C", item.option_c],
    ["D", item.option_d],
  ];

  const optionsHtml = options
    .map(
      ([label, text]) => `
      <label class="option option-choice">
        <input type="radio" name="answer-${esc(item.question_id)}" value="${label}">
        ${label}. ${esc(text)}
      </label>`
    )
    .join("");

  return `
    <div class="question-card">
      <div class="q-header">
        <span class="skill-name">${index + 1}. ${esc(item.skill)} (Level ${item.target_proficiency_level}${item.is_critical ? ", critical" : ""})</span>
        ${item.is_synthetic_grounding ? `<span class="badge pending" title="Grounded on placeholder data, not real SME-authored content">Synthetic grounding</span>` : ""}
      </div>
      <div class="meta">Difficulty: ${esc(item.difficulty)}</div>
      <div class="q-text">${esc(item.question_text)}</div>
      <div class="options">${optionsHtml}</div>
    </div>`;
}

function renderSkipped(skipped) {
  if (!skipped || !skipped.length) return "";
  return `
    <div class="skipped-box">
      <strong>${skipped.length} skill(s) skipped</strong> (no question generated for these -- shown so nothing is silently dropped):
      <ul>${skipped.map((s) => `<li><strong>${esc(s.skill)}</strong> -- ${esc(s.reason)}</li>`).join("")}</ul>
    </div>`;
}

function renderCandidates(candidates) {
  return `<div class="candidate-list">` +
    candidates
      .map(
        (c) => `<div class="candidate-card">
          <strong>${esc(c.role_name)}</strong> ${frameworkBadge(c.framework)}
          <div class="meta">${esc(c.role_id)} &middot; Grade ${esc(c.role_grade)} &middot; ${c.skill_count} skills</div>
        </div>`
      )
      .join("") +
    `</div>`;
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();

  if (!fileInput.files[0]) return;

  resultsArea.innerHTML = "";
  submitBtn.disabled = true;
  setStatus("info", `<span class="spinner"></span>Matching role and generating questions&hellip; this can take 15&ndash;30s for several items.`);

  const formData = new FormData();
  formData.append("file", fileInput.files[0]);
  formData.append("max_items", document.getElementById("max-items").value || 5);

  try {
    const res = await fetch("/api/generate", { method: "POST", body: formData });
    const data = await res.json();

    if (!res.ok) {
      setStatus("error", esc(data.message || "Something went wrong."));
      return;
    }

    if (data.status === "extraction_failed") {
      setStatus("error", esc(data.message));
    } else if (data.status === "no_match_found") {
      setStatus("warn", esc(data.message));
    } else if (data.status === "blueprint_unavailable") {
      setStatus("warn", esc(data.message));
    } else if (data.status === "needs_human_confirmation") {
      clearStatus();
      resultsArea.innerHTML =
        `<div class="status-box warn">${esc(data.message)} (distance gap: ${data.gap.toFixed(4)})</div>` +
        renderCandidates(data.candidates);
    } else if (data.status === "questions_unavailable") {
      clearStatus();
      resultsArea.innerHTML =
        renderMatch(data.match, null) +
        `<div class="status-box error">${esc(data.message)}</div>`;
    } else if (data.status === "auto_matched") {
      clearStatus();
      currentItems = data.items;
      currentMatch = data.match;
      resultsArea.innerHTML =
        renderMatch(data.match, data.counts) +
        `<div id="question-forms">${data.items.map(renderQuestionForm).join("")}</div>` +
        (data.items.length
          ? `<button type="button" id="answer-submit-btn" class="answer-submit">Submit Answers</button>`
          : "") +
        renderSkipped(data.skills_skipped);

      const answerBtn = document.getElementById("answer-submit-btn");
      if (answerBtn) answerBtn.addEventListener("click", submitAnswers);
    }
  } catch (err) {
    setStatus("error", "Request failed: " + esc(err.message));
  } finally {
    submitBtn.disabled = false;
  }
});

let currentItems = [];
let currentMatch = null;

async function submitAnswers() {
  const answers = {};
  for (const item of currentItems) {
    const checked = document.querySelector(`input[name="answer-${item.question_id}"]:checked`);
    if (checked) answers[item.question_id] = checked.value;
  }

  const answerBtn = document.getElementById("answer-submit-btn");
  if (answerBtn) answerBtn.disabled = true;
  setStatus("info", `<span class="spinner"></span>Scoring your answers&hellip;`);

  try {
    const res = await fetch("/api/score", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: currentItems, answers }),
    });
    const scoreResult = await res.json();

    if (!res.ok) {
      setStatus("error", "Scoring failed.");
      if (answerBtn) answerBtn.disabled = false;
      return;
    }

    // Results render on their own analytics page, not inline here --
    // handed off via sessionStorage since there's no server-side session.
    sessionStorage.setItem(
      "talent360i_results",
      JSON.stringify({ match: currentMatch, items: currentItems, score: scoreResult })
    );
    window.location.href = "/results";
  } catch (err) {
    setStatus("error", "Scoring request failed: " + esc(err.message));
    if (answerBtn) answerBtn.disabled = false;
  }
}
