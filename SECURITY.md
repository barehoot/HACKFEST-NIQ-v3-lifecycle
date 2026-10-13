# Talent 360i — Security, Responsible-AI & Data-Privacy Evidence

Maps the Hackfest 2026 guidelines and the *Guidelines for Secure Coding and implementing secure
controls during Implementation (v1.0)* to what is implemented in this repo and where the proof is.
Run the evidence yourself: `python -m pytest tests -q` (147 tests, no network/LLM needed).

## 1. Architecture and trust boundaries

```
Browser ──HTTPS/session+CSRF──▶ Portal (Talent360i_phase11C…/app.py, RBAC, SQLite users/cycles)
                                   │  Bearer token, https (loopback http allowed)
                                   ▼
                        Pipeline API (app/main.py)  ── validates upload/JSON, rate-limits, audits
                                   │
             agents/ (A1 match → A2 questions → A3 score → A4 evidence → A5 plan)
                                   │  single choke point: agents/llm_client.py
                                   ▼
                        CIS / Azure AI Inference gateway (approved provider)
```
Trust boundaries: browser↔portal, portal↔API, API↔LLM, and **all LLM output** (untrusted).

## 2. Guideline → control map

| Guideline | Control | Where | Test |
|---|---|---|---|
| A.2 input validation / whitelist | Upload allowlist (`.pdf/.docx/.txt`), 5 MB cap, magic-byte check, safe filenames; Pydantic bounds on every API field (ranges, id regex, sizes) | `agents/security.py`, `app/main.py`, portal `services/security.py` | `TestUploadValidation`, `TestPipelineAPI` |
| A.2 SQL injection | Parameterised queries everywhere; the single dynamic `UPDATE` checks column names against a fixed allowlist (values always bound) | `services/db.py` | `test_update_cycle_rejects_unknown_columns`, bandit |
| A.2 / K XSS | Jinja autoescape (the `safe` filter is never used); portal JS never uses `innerHTML`; LLM output stripped of markup/script/control chars before it reaches any caller | `security.sanitize_output`, `templates/` | `TestOutputSanitisation` |
| A.2 / A.7 no hardcoded secrets | All keys from env; `.env` git-ignored; demo/synthetic passwords are required env vars (no hardcoded default). `start.py` fills any *empty* secret (API token, session secret, passwords) with a random value written to `.env` (never printed); the portal no longer has a hardcoded session-secret fallback | `.env.example`, `start.py`, `scripts/seed_users.py`, portal `app.py` | grep / TruffleHog in CI |
| A.7 no secrets in logs/errors | `redact()` on all logged text; audit details key-masked; API errors are generic (no exception text, URLs, keys); portal runs with Starlette `debug=False` always, so tracebacks never reach the browser | `security.redact`, `app/main.py`, portal `app.py` | `test_errors_do_not_leak_internals`, `test_audit_masks_sensitive_fields` |
| A.3 dependencies | Every package pinned: `requirements.in` (direct) compiled to a full cross-platform lock `requirements.txt` (113 packages); `pip-audit` (SCA) + `bandit` (SAST) + TruffleHog in CI, and the build fails on findings; bandit is clean (4 reviewed `nosec` lines, each with a justification comment) | `requirements.in`, `requirements.txt`, `.github/workflows/security.yml` | CI |
| A.4 / B.2 authN | bcrypt hashes; login lockout (5 fails → 5 min); **session regenerated on login**; idle (30 min) + absolute (8 h) timeouts; password policy for new accounts | `services/auth.py`, `services/security.py`, `app.py` | `TestPortal` |
| A.4 / B.3 authZ (RBAC) | `require_role` on every portal route; **manager decisions restricted to own queue** (IDOR fixed); **workflow state guards in SQL**: a cycle can only be manager-decided from `pending_manager_approval` (not even admin can skip the SME gate / assessment) and an SME decision only applies to a *pending* question; pipeline API requires bearer token (constant-time compare, fail-closed outside debug) | `app.py`, `services/db.py`, `app/main.py` | `test_requires_bearer_token`, `tests/test_portal_workflow.py` |
| B.1 transport | Portal→API must be `https://` (loopback exempt); LLM endpoint must be `https://`; HSTS + secure cookies when not in debug | `backend_api.py`, `llm_client.py`, `app.py` | – |
| B.4 injection classes | Path traversal (basename + charset), multipart-boundary/filename injection, command execution (none used) | `security.safe_filename`, `backend_api.upload_resume` | `test_path_traversal_filename_is_neutralised` |
| B.5 / C.6 logging & monitoring | Append-only JSONL audit (`logs/audit.jsonl`, `logs/llm.jsonl`, `logs/portal_audit.jsonl`): API calls, auth failures, lockouts, rate-limits, role changes, manager decisions, LLM calls | `security.audit`, `governance.py` | `test_prompt_hash_logged_not_prompt_text` |
| C.7 DoS | Per-client rate limits, request/upload/prompt size caps, LLM timeouts, token budgets (`governance.py`) | `app/main.py`, `llm_client.py` | `test_rate_limit` |
| D transparency | Dismissible "AI notice" banner on every portal page that shows AI-generated content (employee dashboard/assessment/case study/training plan, SME generation & review, manager validations, learning pages); `ai_generated`/`ai_disclosure`/`human_review_required` on every AI API response and item | `templates/partials/ai_notice.html`, `base.html`, `app/main.py` | – |
| E approved providers | Non-`cis` provider **refused** when `TALENT360_ENV` is production/staging. No generative model is downloaded. The only downloaded model is Chroma's default sentence-embedding model (all-MiniLM-L6-v2, Apache-2.0) in **ONNX** format, which is a protobuf graph with no pickle / arbitrary-code loading; it is fetched once by `scripts/build_chroma.py` (see section 6) | `llm_client._check_provider_allowed`, `scripts/build_chroma.py` | `test_unapproved_provider_blocked_in_production` |
| F hallucination / misinformation | Output restricted to strict JSON schemas; A2 items validated + self-checked + **SME approval gate**; A4 quotes must literally occur in the candidate's answer; invalid verdicts downgraded; A5 course IDs verified against catalogue; **manager gate** before any outcome | `agent2/4/5`, portal workflow | `TestEvidenceAgent`, `TestQuestionAgent` |
| G jailbreak / prompt injection | Untrusted text (resume, answers) restricted to letters/numbers/punctuation/symbols and length-capped; wrapped in `UNTRUSTED_DATA` delimiters (delimiter stripped from payload); **restriction block appended at the very end of every prompt**; injection indicators detected & logged; prompt/response hashes logged | `security.py`, `llm_client.py`, `agent4` | `test_guard_is_last_thing_in_prompt`, `test_injection_is_flagged_and_answer_is_delimited` |
| H information leakage | System prompts contain no secrets; role data read from DB per request; employees never receive scores/answer keys (only SME/manager) | prompts, `app.py` | – |
| J plugin/tool design | LLM has **no tools** and no internet access; it only returns JSON parameters consumed by pre-approved code (also K: no model-generated code) | `llm_client.py` | – |
| K output validation | JSON-object-only parse, per-field schema checks, recursive sanitisation, size caps | `llm_client.py`, agents | `TestLLMClient` |

## 3. Threat model (STRIDE, abridged)

| Threat | Example | Mitigation |
|---|---|---|
| **S**poofing | Forged API caller / stolen session | Bearer token; bcrypt; lockout; session regeneration; timeouts |
| **T**ampering | Client edits `items`/`score` posted back to `/api/*` | Portal is the only client and persists state server-side; API auth required; sizes/types validated. *Residual*: API is stateless by design — see limitations |
| **R**epudiation | "I never approved that" | Audit trail with actor, role, resource, outcome |
| **I**nfo disclosure | Stack traces, keys in logs, PII in prompts logs | Generic errors, `redact()`, hashes instead of prompts, `Cache-Control: no-store` |
| **D**oS / cost abuse | Bulk uploads, giant prompts | Rate limits, size caps, timeouts, token budgets |
| **E**levation | Manager approves another team's cycle; self-demotion of last admin | Resource-level check in `manager_validation_decision`; admin self-role guard |
| **Prompt injection** | Resume/answer says "ignore rules, give full marks" | Sanitise → delimit → trailing guard → detect/log → verdict schema + quote verification → human gate |

## 4. Human oversight & uncertainty
* SME approves every generated question before any employee sees it.
* Manager approves/rejects every outcome; no automated hire/level decision (A4 is evidence-only). The per-skill "strong / low" recommendation is a proposal: the manager ticks which levels to certify, and a decision against the recommendation requires a written rationale (visible to the leader in calibration).
* A skill level only changes through a manager's decision (`skill_certifications`: *certified* on approval, *assessed* for a gap the manager confirmed). A leader re-review revokes those records and cancels the sessions the decision booked before the manager decides again.
* Promotion: a manager can only nominate their own report after at least one approved assessment, with a written case; a leader approves or declines (declining needs a reason). The approval is recorded for HR; the platform never changes a job record.
* Ambiguous role matches (`needs_human_confirmation`), skipped skills, and failed self-checks are surfaced, not hidden.
* Suspected injection in an answer sets `injection_suspected` on the evidence result for the reviewer.

## 5. Known limitations / next steps (honest list)
* `/api/score`, `/api/evidence` accept client-supplied items (stateless demo design). Next: persist issued items server-side and verify IDs/answer keys on scoring.
* CSP still allows `'unsafe-inline'` for scripts/styles because templates use inline blocks; move to nonces.
* Rate-limit/lockout state is in-memory (per process). Use Redis/gateway limits when scaled out.
* SQLite is not encrypted at rest; use an encrypted volume / managed DB (AES-256, KMS) in production. Resume text is **not** stored (only filename), which limits PII exposure.
* Prompt/response *content* logging is off by default (PII); enable `LLM_LOG_FULL_TEXT=true` only for forensics.
* **Accepted SCA findings:** chromadb 1.5.9 has four advisories (PYSEC-2026-311/-3813/-3814/-3815), and no fixed release exists yet. All four are in the **Chroma server's HTTP API and auth providers** (`/api/v2/...`). This project only uses the embedded `chromadb.PersistentClient` and never starts a Chroma server, so none is reachable. They are ignored by ID in CI; re-check when chromadb ships a fix.
* Leader *movement over time* and *item analysis* screens show synthetic sample figures from `services/api_client.py` (no assessment history exists yet). Manager calibration, the organisation report and the manager team screens are computed live (`services/insights.py`, `services/tni.py`).
* **Agent 5 training outline** (`agents/agent5_outline.py`, `POST /api/training-outline`). Only skills, levels and Agent 4's short "missing element" note leave the portal (no name, transcript or resume); the note is wrapped as untrusted data. Output is verified deterministically (every skill covered, ≤3 sessions per skill, only catalogue courses mapped to that skill, allowed formats and durations, objectives grounded in the level indicator), revised once, and replaced by a labelled template if it still fails or the model is unavailable, so a manager's decision never depends on the LLM.
* The promotion ladder is derived from `role_master` (next grade up in the same tower, preferring the same process group); it is decision support, not an HR succession model. The dataset marks role-readiness signals as a roadmap item needing HR/legal approval (Roadmap-02).
* Agent 4 runs a multi-turn workplace simulation on the weakest *assessed* skills: up to 3 probes per scenario (cap enforced in `agents/agent4_evidence.next_probe`, not trusted to the model), every candidate turn delimited as untrusted data and scanned for injection, and every quoted phrase verified against the candidate's own words. The pipeline API stays stateless; the portal persists the transcript per cycle. The legacy `domain/a4_evidence.py` probe module is superseded and unused.
* **AI-use (integrity) check in the simulation.** Signals: the portal's browser telemetry (characters pasted, time from first keystroke to submit), AI-typical wording and formatting, and the evidence model's own judgement. A high combined risk halves that scenario's evidence credit and medium reduces it by 20%; the raw score, the adjusted score and every reason are shown to the manager and leader. Limits: telemetry is client-side and can be bypassed (e.g. JavaScript disabled removes it, leaving only the text signals); AI-text detection has false positives, particularly for formal or non-native writers. It is a flag for the human reviewer, never a decision on its own.
* **Question bank.** Approved questions are reused across assessments (`bank_questions`); deployed copies keep the original SME approval and a `bank_question_id`. The automatic assessments (first evaluation for the current role, retake, re-assessment after gap training, promotion readiness) only ever use already-approved questions; `scripts/seed_question_bank.py` loads the dataset's SME-approved questions (and its pending ones into the SME queue) so every role has some. The older in-memory demo bank behind the scheduler's BP-1001 launch gate is still separate.

## 6. Process items (cannot be enforced in code — track in the team checklist)
- [ ] MFA on the repository; peer security review of PRs; security champion named.
- [ ] Cycode/SAST scan run and results attached (CI workflow provides `bandit`, `pip-audit`, TruffleHog as baseline).
- [ ] Legal approval for any open-source model licence (incl. the all-MiniLM-L6-v2 embedding model, Apache-2.0), and confirmation that its ONNX format is acceptable under the safetensors-only rule; only Azure AI Services-hosted generative models.
- [ ] Azure AI content filters (incl. jailbreak filter) enabled on the CIS deployment.
- [ ] Data-classification / privacy review for resume (PII) handling and retention.
- [ ] Set `TALENT360_DEBUG=false`, `TALENT360_ENV=production`, strong `TALENT360_SESSION_SECRET` and `TALENT360_BACKEND_TOKEN` for the demo/deploy environment.

## 7. v3 employee lifecycle: performance, evidence, probation (added Oct 2026)

Performance management is **high-risk AI use** (EU AI Act Annex III, employment). Every design choice below
keeps people deciding and the person affected informed. Evidence: `tests/test_lifecycle.py` (21 tests) and
`tests/test_lifecycle_agents.py` (21 tests); all run without network or an LLM key.

| Risk | Control | Where | Test |
|---|---|---|---|
| Secret scoring of people | The status is **deterministic and explainable** (five weighted factors, each with a reason); the employee sees the same factors as the manager on *My Performance* | `services/performance.py`, `employee/performance.html` | `test_status_engine_counts_only_factors_with_data` |
| Hidden evidence moving a rating | Only evidence **shared with the employee** counts; private manager notes are kept but never scored; the employee can reply to every shared item | `performance.evidence_factor`, `lifecycle_store.set_evidence_comment` | `test_low_review_caps_band_and_hidden_evidence_never_counts`, `test_employee_can_reply_to_shared_evidence` |
| Missing data penalising people | Factors without data are left out and listed as "not counted against them"; fewer than two factors gives *Not enough data*, never a low band | `performance.compute` | same |
| Wellbeing data used against people | Pulse check-ins never change the score; the per-person wellbeing flag is visible to the **line manager only** (leaders, admins and the AI never see it) | `performance.visible_signals`, `insight_payload` | `test_wellbeing_is_a_signal_never_a_score`, `test_wellbeing_flag_reaches_the_manager_only` |
| Workplace surveillance | **No mailbox or chat is scanned.** Only a message a manager deliberately tags (upload, paste, or an Outlook category / Teams action flow) reaches Talent 360i. Emails, phone numbers and long number runs are masked; only a 1,500-character excerpt is kept, never the raw message or attachments; a content hash blocks double counting | `services/evidence.py` | `test_tagged_email_is_redacted_classified_and_waits_for_confirmation` |
| AI deciding about people | Agent 7 only **suggests** how to record a message: nothing is recorded until the manager confirms or edits it. Agent 6 never sets the band, may not recommend dismissal, and may not mention protected characteristics (verification fails, retries once, then a labelled template). Escalations are suggestions for a human review. Probation outcomes need a manager recommendation **and** a leader decision; the platform records them for HR and never changes an employment record | `agents/agent6_performance.py`, `agents/agent7_evidence.py`, `services/probation.py` | `test_a6_rejects_dismissal_language_and_falls_back`, `test_a6_escalation_must_agree_with_the_band`, `test_probation_from_hire_date_to_leader_decision` |
| Unfair negative probation outcome | "Do not confirm" needs at least two documented check-ins and a written rationale; the employee reads every check-in note; a leader can return a recommendation | `probation.recommend` | `test_probation_from_hire_date_to_leader_decision` |
| Data minimisation to the model | Agent 6 receives role, band, factor scores, skill levels and evidence *titles* with the employee's name replaced; no name, ID, email or manager. Agent 7 receives the redacted excerpt and the role's skill list | `performance.insight_payload`, `evidence.import_message` | `test_review_share_and_acknowledge_locks_it` (asserts no id/name in the payload) |
| Prompt injection via emails / Teams / code | All tagged text and submitted code is delimited untrusted data; injection phrases are flagged and audited; code is wrapped by a whitespace-preserving variant that still strips control/bidi characters and the delimiter | `agent7_evidence.classify`, `agent4_code_review.wrap_code` | `test_a7_flags_injection_in_the_message`, `test_code_review_prompt_keeps_indentation_and_its_delimiters` |
| Running untrusted code | Coding answers are **never executed**: static checks (risky calls, unchanged starter code, text aimed at the reviewer) plus an LLM rubric review; flagged answers always wait for an SME; a verdict can only be downgraded to match the scores | `agents/agent4_code_review.py`, `services/coding.py` | `test_code_review_never_executes_and_flags_risky_calls`, `test_code_review_verdict_is_only_downgraded` |
| Resume retention | Coding triggers are matched inside the pipeline API; only the SME keywords the resume mentions come back and are stored (`cycle_resume_signals`), never the resume | `app/main._keyword_hits`, `services/coding.py` | `test_match_reports_only_probe_keyword_hits`, `test_resume_keyword_triggers_coding_task_reviewed_never_run` |
| Broken access control (IDOR) | Every people route checks scope: line manager for their reports, department leader for their departments, admin; a miss is a 404 and is audited. Leaders read but don't record evidence. Employees can only update their own actions, comments and reviews | `routes/web.person_in_scope` | `test_people_records_are_scoped`, `test_department_scoped_leader`, `test_plan_merges_verified_ai_actions_without_duplicates` |
| Job-requirement tampering | Managers can only *propose* requirement changes; admins and leaders apply them; every change records who, when and why (optionally linked to a market trend); the last skill of a role can't be removed | `services/job_profiles.py` | `test_manager_proposes_leader_applies_and_tni_follows_the_role` |
| Integration endpoint abuse | `/integrations/evidence` is **off** unless `TALENT360_INTEGRATION_TOKEN` is set; constant-time bearer check; 64 KB body cap; the tagger must be the employee's manager; items land as pending | `routes/platform.py` | `test_integration_ingest_requires_token_and_the_manager` |
| Open redirect | Notification links are followed only when they are in-app paths | `routes/platform.notification_open` | `test_notifications_bell_and_safe_links` |

**Limits to know about.** The evidence classifier's keyword fallback is crude (it is only used when the model is
unavailable, and the manager confirms every suggestion). Name scrubbing for Agent 6 replaces the employee's own
name tokens; other people named in a manager's evidence title can still reach the model, so titles should state
facts, not names (the form says so). Status bands are decision support whose thresholds (80 / 65 / 50) and
weights need calibration with HR on real data before any rollout; a DPIA and works-council consultation are
required in jurisdictions that mandate them before evidence tagging or pulse check-ins go live.

## 8. v3.1 organisation design: HR role, onboarding, Agent 8 skill advisor (added Oct 2026)

| Risk | Control | Where | Test |
|---|---|---|---|
| AI changing job requirements | Agent 8 only **pitches** skills: a pitch is stored as *pending* and nothing applies until a person ticks it. An admin, HR partner or leader applies it as a normal recorded requirement change (who, when, why: "Agent 8 recommendation, reviewed by a person"); a manager's acceptance becomes a proposal. New catalogue skills need an admin or HR partner | `services/skill_advisor.py`, `routes/org_admin.py` | `test_onboarding_creates_vertical_department_role_and_reviews_the_ai_pitch`, `test_manager_pitch_becomes_proposals_and_an_unavailable_advisor_stores_nothing` |
| Hallucinated or vague requirements | Deterministic verification before anything is shown: a catalogue skill must be flagged existing (and keeps the catalogue spelling), target 1–5, at most 3 critical, a 20–400 character behaviour indicator, no duplicates or already-required skills; catalogue mode never re-proposes an existing skill and needs five L1–L5 indicators. One revision, then a **labelled** rules fallback. The portal re-checks names and levels again, and a new requirement still needs a concrete behaviour indicator (the person must improve a vague one) | `agents/agent8_skill_advisor.py`, `services/skill_advisor.py`, `job_profiles.request_change` | `test_a8_bad_pitch_is_revised_then_falls_back_to_rules`, `test_a8_catalogue_never_reproposes_and_rules_use_trends_then_watchlist` |
| Prompt injection via role text, trends or focus | Role summary, responsibilities, trends and the requester's focus are delimited untrusted data; injection phrases are flagged and audited; no personal data is sent (only the role text, the catalogue's skill names and logged trends) | `agent8_skill_advisor.advise` | `test_a8_verified_role_pitch_keeps_catalogue_spelling`, `test_a8_flags_injection_and_rejects_bad_input` |
| Over-broad HR access | The `hr` role reads people, structure and skills organisation-wide, but **records nothing about an individual**: evidence, reviews, plans and probation check-ins stay with the line manager (403), escalations and AI insights are blocked for HR (403), probation decisions stay with leaders (the tracker is read-only for HR). Only an admin grants the role; the change is audited | `routes/lifecycle_people._not_read_only`, `routes/web.person_in_scope`, `app.admin_update_user_role` | `test_hr_workspace_reads_org_wide_but_records_no_performance`, `test_admin_grants_hr_access_and_a_reassignment_moves_the_team` |
| Half-created organisation records | Onboarding validates every field (grade, summary, duplicate vertical code/name, duplicate department, duplicate job title in the vertical, leader) **before** creating the vertical, the department or the role | `routes/org_admin.org_onboard_submit` | `test_onboarding_validates_before_creating_anything` |
| Schema change on an existing database | The `users.role` CHECK gains `hr` through SQLite's documented create–copy–drop–rename rebuild, once, inside `BEGIN IMMEDIATE`, with foreign keys off only for that transaction; row ids and indexes are kept; idempotent and safe if two processes start at once | `services/db.ensure_user_roles`, `scripts/seed_users.migrate_user_roles` | `test_pre_hr_database_is_migrated_in_place` |
| Calendar file injection | `.ics` export escapes `\`, `;`, `,` and turns every newline into the two characters `\n`, so a booking title or note can never start a new calendar property; lines folded at 75 octets; employees export only their own bookings, the scheduler one month | `services/schedule.to_ics` | `test_calendar_agenda_collapses_and_exports_ics` |
| Stale manager grouping | Leaders' and HR's "people by manager" view reads the status snapshot; an admin's manager change re-scores that employee at once, and someone who stops being an employee leaves the snapshot | `app.admin_update_user_role`, `lifecycle_store.drop_status` | `test_admin_grants_hr_access_and_a_reassignment_moves_the_team` |

**Limits to know about.** Agent 8's rules fallback is keyword overlap with the catalogue plus the role's own
plain-words skills: useful when the model is down, not a substitute for it. "Needs support" (40%+ of a team of 3+
at risk or off track) and "Wide span" (more than 15 reports) are prompts for a conversation, not ratings of the
manager; the thresholds are constants in `routes/hr.py` to calibrate with HR.

## 9. v3.2 performance summaries, CV evidence and internal mobility (added Oct 2026)

| Risk | Control | Where | Test |
|---|---|---|---|
| An AI summary inventing or overstating events | Agent 9 summarises only the record it is given; every highlight / concern must cite evidence refs that exist (the portal drops any other ref again); a rating suggestion must sit in the band's range; the promotion view must repeat the rules' readiness exactly; one revision, then a labelled rules template. The rules-built fact sheet is always shown beside it | `agents/agent9_performance_summary.py`, `services/performance_summary.py` | `test_a9_rejects_invented_refs_out_of_range_rating_and_banned_language`, `test_a9_promotion_view_must_repeat_the_rules`, `test_summary_is_de_identified_cites_real_evidence_and_starts_a_review` |
| Personal data to the model | Agent 9 receives evidence titles / contexts, reviews and skills with the employee's name replaced; no id, email or manager. Private manager notes are counted but never sent. Agent 7 sees the manager's note and the role's skill list only | `performance_summary.payload`, `performance.scrubber` | `test_summary_is_de_identified_cites_real_evidence_and_starts_a_review` |
| AI rewriting the record | Agent 7's reading of a manager note is stored beside it (`ai_json`), never over the kind, tone or text; a disagreement is shown as a tone check for the manager | `evidence.evaluate` | `test_manual_note_is_evaluated_by_agent7_without_changing_it` |
| CV retention / hallucinated CV skills | The pipeline API reads the CV, sends it to Agent 8 as delimited untrusted data, and returns only claims: skills from the requested list, levels 1–5, quotes that must appear verbatim in the CV (else keyword rules). The portal stores the claim and the quote only, for the skills that matter to the person, and the employee can delete them. A CV claim is labelled as such everywhere and never counts towards promotion readiness | `app/main.resume_skills`, `agent8_skill_advisor.assess_resume`, `mobility.save_cv_claims` | `test_cv_reader_needs_verbatim_quotes_and_listed_skills`, `test_resume_skills_requires_token_validates_and_returns_only_claims`, `test_aspiration_gap_analysis_and_cv_claims` |
| Talent pool exposing people | Candidates are people who aspired or applied (opted in). "Potential fits" appear only on request, found from recorded skills, and show readiness without performance band, readiness level or CV quotes unless the viewer already has scope over them (their manager, their department's leader, HR, admin). Every talent-card view is audited; the full 360 profile link appears only for viewers already in scope | `mobility.talent_pool`, `routes/career.talent_card` | `test_potential_fits_show_no_performance_outside_scope`, `test_application_notifies_and_the_hiring_side_decides` |
| Unfair hiring decisions | Declining an internal application needs a note the applicant reads; the applicant's own manager is told about the application (and can't block it); a move still goes through a nomination and a leader decision, or the normal hiring process | `mobility.decide_application`, `mobility.apply` | `test_application_notifies_and_the_hiring_side_decides`, `test_cross_vertical_assessment_and_nomination_need_an_aspiration` |
| Calling an external job portal | The SmartRecruiters adapter accepts only an https `smartrecruiters.com` base, has a 10 s timeout and a 2 MB response cap, parses JSON only, reduces job-ad HTML to sanitised text (scripts dropped), and never logs or shows the token. A failed sync keeps the last good copy | `services/smartrecruiters.py` | `test_live_adapter_parses_postings_and_only_talks_to_smartrecruiters` |
| HR mapping overwritten by automation | Title auto-mapping never replaces a mapping an admin / HR partner made; it survives every re-sync | `lifecycle_store.upsert_posting` | `test_demo_postings_sync_map_and_survive_resync` |

**Limits to know about.** The SmartRecruiters adapter follows the documented Posting API shape and is tested
against recorded payloads, not a live tenant: check the field mapping in `parse_posting` (one function) and the
`destination` filter against your tenant before go-live. Skill names must match between job profiles for levels to
carry across roles (exact catalogue names); the CV reader's keyword fallback is deliberately conservative (L2 only).
