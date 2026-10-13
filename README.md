# Talent 360i — Auto Assessment Generation with SME in the Loop

AI agents turn NIQ FinOps role/skill data into skill- and level-matched assessments. People
make every decision: an **SME approves each AI-generated question** before anyone sees it, and a
**manager confirms the AI's evidence evaluation** before any training plan or level change.

All employee data is the synthetic Hackfest dataset in `data/`.

## What's new in v3: the employee lifecycle

The winning assessment loop now feeds a full performance and lifecycle system, built for 20,000+ employees.

| | What it does | Who decides |
|---|---|---|
| **On track / off track status** | Every employee scored from five explainable factors: skills vs the live job profile, the latest review, the latest assessment, evidence (180 days) and development follow-through. Four bands, Exceeding (best), On track (good), At risk (low), Off track (poor), plus *Not enough data*. Rules, not AI | Managers and leaders read it; it decides nothing |
| **Evidence tagging** | Appreciations, achievements, 1:1 notes, feedback, escalations and concerns, logged by the manager, uploaded from Outlook (`.eml`), pasted from Teams, or posted by a Power Automate flow. **Agent 7** suggests the kind, tone, skills and a factual summary | The manager confirms every item; the employee sees and can reply to shared ones |
| **Performance reviews** | Periodic reviews with an **Agent 6** draft built from the evidence; the employee acknowledges and comments | The manager writes and shares |
| **Development plans (IDP)** | Actionable items with due dates and success measures, generated from skill gaps (real catalogue courses), escalations, review focus areas and next-role stretch work, plus verified Agent 6 suggestions | Manager and employee; overdue actions escalate |
| **Escalation** | At risk or off track while the plan stalls → the manager; off track for 30+ days despite a plan → the business leader; resolves itself when the status recovers | People review; the system only suggests |
| **Probation (6 months)** | Tracked from the hire date, with check-ins at months 1, 3 and 5, reminders, and a recommendation to confirm, extend or not confirm | Manager recommends, business leader decides |
| **Promotion readiness level** | Next-role skills readiness **and** performance: Ready now / Ready in 6–12 months / Not yet / On hold | Existing nomination flow |
| **Employee cards** | Pop-out cards for managers and leaders (status, score, five factor bars, signals); click for a quick-view dialog, open for the full 360 profile | – |
| **Job profiles & trends** | Add job profiles and departments, change required skills and targets as the market moves, log trends; TNI, scoring and question generation follow the live profile | Managers propose; admins and leaders apply |
| **Resume-triggered coding tasks** | SMEs write coding problems with trigger keywords (python, sql, vba…); a matching resume adds them to the workplace simulation; reviewed by the **Agent 4 code reviewer** (never run) | SME confirms the score |
| **Engagement & reach** | Notification inbox (bell), email/Teams outbox, weekly pulse check-in (wellbeing only, never scored), peer recognition | – |
| **Scale** | Status snapshot read model, indexed signals, a background worker, `/healthz` `/readyz`, Docker Compose, a 20k/40k generator and benchmark | See [deploy/README.md](deploy/README.md) |

## What's new in v3.1: organisation design

| | What it does | Who decides |
|---|---|---|
| **Onboard a job profile** | A guided flow: vertical (existing or **new**, e.g. FP&A) → department (existing or new, with its leader) → job profile → **Agent 8 pitches the required skills** → a readiness checklist (department, 3+ skills, a critical skill, concrete behaviour indicators, SME-approved questions, mapped training). SMEs are notified when a new role needs questions | Admins, HR partners and leaders |
| **Agent 8, skill advisor** | Pitches the skills a job profile should require (catalogue skills first, new ones only where needed), each with a target level, criticality, the behaviour expected and why; and recommends **new skills for the catalogue** from logged market trends. Verified, revised once, labelled rules fallback | A person ticks, edits and applies each pitch; a manager's pick becomes a proposal |
| **Skill catalogue** | Every skill with L1–L5 indicators and how many roles require it; add skills by hand or accept Agent 8's recommendations | Admins and HR partners |
| **HR Business Partner role** | An HR workspace: headcount, verticals and departments, teams that need support, the onboarding pipeline, probation tracker, HR attributes. Reads people organisation-wide; records nothing about an individual's performance | Admins grant HR access (Users) |
| **People by manager** | Leaders, HR and admins open *People* on one tile per line manager (team size, band mix, average score, escalations, probation, "needs support" / "wide span" prompts); click a manager to see the employees reporting to them. Search still lists people | – |
| **Calendar** | The agenda can be minimised and expanded (whole agenda or day by day, remembered per browser); bookings export to Outlook / Google / Apple as `.ics` | – |

## What's new in v3.2: performance summaries and internal mobility

| | What it does | Who decides |
|---|---|---|
| **Every note read by AI** | A note the manager writes now goes through **Agent 7** too (before, only tagged emails / Teams chats did): the skills it shows, a factual one-line context and a tone check when the reading disagrees with how it was logged. The record itself is never changed | The manager |
| **Agent 9, performance summary** | For a **mid-year review, annual review, 1:1 or promotion discussion**: one summary from manager reviews, evidence tagged from Outlook and Teams, manager notes (with Agent 7's context), skills achieved, the learning curve and training. Every highlight cites its evidence; the promotion view repeats the rules' readiness; a rules-built **fact sheet** sits beside it. A mid-year / annual summary can start the review form | Manager writes the review; leader reads; HR reads |
| **Career aspirations across verticals** | Employees pick up to 5 target roles in any department or vertical (e.g. *P2P Senior Associate → Data Analyst*). Gap analysis per role from validated levels, then current-role assessments, then **skills claimed on the CV**; the catalogue course for each gap; a skill-up path and projected learning curve | The employee |
| **CV skill evidence** | Agent 8 reads the CV for the skills that matter to the person and records a claimed level with a quote that must appear **word for word** in the CV. The CV itself is never stored. CV claims never count towards promotion readiness | – |
| **Upskilling on the calendar** | One click books the gap courses on the employee's calendar (and `.ics` export); the manager is told | The employee |
| **Cross-department promotion** | Readiness (Ready now / 6-12 months / Not yet / On hold) for any aspiration role; the manager can assign a readiness assessment for that role or nominate across departments; the leader decides as before | Manager → leader |
| **Internal jobs (SmartRecruiters)** | Open internal postings from the SmartRecruiters Posting API (demo feed until `SMARTRECRUITERS_COMPANY_ID` is set), auto-mapped to job profiles by title or mapped by HR; an unmapped posting can be onboarded as a job profile from its JD. Employees see their fit, add it as an aspiration and apply | HR maps postings |
| **Talent pool** | Hiring managers, leaders and HR see who aspires to or applied for a role, ranked by readiness, **their own team first, then the rest of the organisation**; optionally "potential fits" from recorded skills (no performance data unless already in scope). Talent cards are audited; shortlist or decline with a note | The hiring manager |

Measured on one laptop with SQLite: **20,000 employees recalculated in 9 s**; leader directory 25 ms, 360 profile
49 ms, manager dashboard 43 ms (HTTP p50). At 40,000: 21 s, 45 ms, 65 ms, 64 ms.

---

## Quick start (about 5 minutes)

**You need:** Python **3.12**, internet access on the first run (packages, plus a small
embedding model), and an LLM key: Groq for a personal machine, or the NIQ CIS / Azure AI gateway.

**macOS / Linux**
```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python start.py --setup        # creates .env files, generates secrets, builds the database
```

**Windows (PowerShell)**
```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python start.py --setup
```

Next, open `.env` in the project root and add **one** LLM key:

```ini
GROQ_API_KEY=gsk_...                 # personal machine (default LLM_PROVIDER=groq)
# or, on the NIQ network / VPN:
LLM_PROVIDER=cis
CIS_API_KEY=Bearer ...
```

Then start both servers:

```bash
python start.py
```

Open **http://127.0.0.1:8010/login**. Press `Ctrl+C` to stop both servers (and the lifecycle worker).

| Log in as | Password |
|---|---|
| `employee`, `manager`, `sme`, `scheduler`, `leader`, `admin`, `hr`, `manager2`, `manager3` | value of `TALENT360_DEMO_PASSWORD` in `.env` |
| `syn-u001` … `syn-u036` (synthetic dataset users) | value of `TALENT360_SYNTHETIC_PASSWORD` in `.env` |

`start.py --setup` generates both passwords the first time, writes them into `.env`, and never
prints them. To choose your own, edit `.env` and run `python start.py --reseed`.

---

## Demo walkthrough (the full human-in-the-loop flow)

| # | Log in as | Do this | What it shows |
|---|---|---|---|
| 1 | `employee` | Dashboard → upload `data/sample_resumes/finops_ap_p2p_senior_associate.pdf` | Agent 1 matches the resume to a role. The employee only uploads; they don't choose the number of questions |
| 2 | `sme` | **AI Generation** → role, level, count → Review Queue → **Approve** | Agent 2 writes and self-checks each question at the level the SME chose; approved questions join the reusable **Question Bank** |
| 2b | `sme` | **Assign Assessments** → **Set up assessment** → adjust the proposed questions → **Deploy** | The selection agent proposes bank questions (priority skills, level, least-used); the SME decides. The bank starts with the dataset's SME-approved questions for all 9 roles. Anyone not yet evaluated in Talent360i for the role they hold (dataset levels alone don't count) gets an assessment built from approved bank questions automatically |
| 3 | `employee` | Dashboard → **Start assessment** → answer → submit | Agent 3 scores it; the score is visible only to the SME and manager |
| 4 | `employee` | **Continue simulation** → work through each scenario and answer the AI assessor's follow-ups | Agent 4 runs a workplace simulation, probing up to 3 times per skill for missing evidence, then builds an evidence report from the whole transcript: an evaluation only, never a decision |
| 5 | `manager` | Validations → open the cycle → review the per-skill outcome → **Approve & certify ticked levels** | The recommendation (strong / low) is only a proposal. Ticked skills become *validated* at the level tested; Agent 5 outlines skill-up sessions towards the next role's targets and books them on the calendar |
| 5b | `manager` | …or **Reject & assign skill-gap training** / **Send back to retake** | Reject records the levels shown as confirmed gaps and books an Agent 5 skill-gap outline; when the manager marks every session delivered, a re-assessment on those skills is auto-assigned. Send back assigns a fresh retake (e.g. suspected AI use) |
| 6 | `employee` | **My Training Plan**, **My Skills & TNI** | The training outline from the manager's decision (objectives, activities, practice task, how it's checked, booked dates), validated levels, and the career path to the next role |
| 7 | `leader` | **Organisation Report**, **Manager Calibration** → Review evidence → Concur / Send for re-review | Live org reporting with CSV export; decisions that run against the evidence are flagged, and a re-review returns the decision to the manager (the leader never overrides it) |
| 7b | `manager` | **Assign Training** → enrol a report in a recommended course, then mark it completed | Enrolments appear on the employee's training plan and calendar |
| 8 | `syn-u001` … / `manager` | **My Skills & TNI** / **Learning & TNI** | Skill heatmap, projected learning curve, and recommended learning with gap coverage from the synthetic TNI baseline |
| 9 | `scheduler` | **Schedule & Calendar** → book, or auto-schedule TNI training | Month calendar of every booked assessment and training session; employees see their bookings on their training plan |
| 10 | `manager` → `leader` | **Promotion Readiness** → open a person → **Assign promotion assessment** / **Send profile for promotion review**; then leader **Promotions** → Approve / Decline | Readiness against the next role up the tower (B3 → B4 → P3 → P4). The leader sees readiness, certified levels, every assessment with AI-use flags and the manager's case. An approval is recorded for HR; job records aren't changed |

**v3 lifecycle walkthrough**

| # | Log in as | Do this | What it shows |
|---|---|---|---|
| 11 | `manager` | **Dashboard** → *Needs your attention* → hover and click the cards | Pop-out cards, quick view, pending Outlook/Teams suggestions, probation steps, escalations, team pulse |
| 12 | `manager` | **People & Performance** → open *Synthetic Associate 017* | The 360 profile: why the status is *Off track*, playbook steps, **✦ Generate insight** (Agent 6), escalations to the manager and the leader |
| 13 | `manager` | Profile → **Evidence** → *Tag an Outlook email or Teams chat* → paste a thread | Agent 7 suggests kind, tone, skills and actions; confirm to record it and turn the actions into development items |
| 14 | `manager` | Profile → **Reviews** → **✦ Draft with AI** → edit → *Share*; then **Development** → **✦ Generate** | Review draft within the band's rating range; IDP from gaps, evidence and verified AI actions |
| 15 | `manager` | *Synthetic Associate 035* → **Probation** → record the overdue Month 3 review; *Employee One* → *Recommend an outcome* | Probation tracked from the hire date with reminders; "do not confirm" needs two documented check-ins |
| 16 | `leader` | **Performance**, **Probation**, **People** | Band mix by department, concerns / promotion-ready / top performers, the leader escalation, approve *Synthetic Associate 036*'s probation |
| 17 | `manager` → `leader` | **Job Profiles & Skills** → *Senior Revenue Associate* → approve the pending *Finance Automation* proposal | Requirements change with a reason and a trend; everyone in the role is re-scored |
| 18 | `sme` | **Coding Challenges** | Three approved challenges (python/sql/vba triggers) and the answer review queue |
| 19 | `employee` | **My Performance** | The same factors the manager sees, shared evidence with *Add your side*, probation timeline, pulse check-in, recognise a colleague |

**v3.1 organisation-design walkthrough**

| # | Log in as | Do this | What it shows |
|---|---|---|---|
| 20 | `leader` | **People** → click *Manager Two* | Teams grouped by line manager (band mix, needs-support / wide-span prompts), then that manager's reports |
| 21 | `hr` | **Onboard a Job Profile** → *＋ New vertical* `FPA` / *＋ New department* → describe an *FP&A Analyst* → keep **✦ Ask Agent 8** ticked | A new vertical, department and role in one step; Agent 8 pitches the required skills |
| 22 | `hr` | Untick what doesn't fit, edit levels / behaviour → **Apply ticked skills** | Requirements applied as recorded changes; the readiness checklist updates; SMEs are notified to generate questions |
| 23 | `hr` | **Skill Catalogue** → **✦ Recommend new skills** → *Add to catalogue* | New skills from logged trends, each with L1–L5 indicators |
| 24 | `admin` | **Users** → set someone's role to *HR Business Partner*, or change an employee's manager | HR access granted; the employee moves to the new manager's team straight away |
| 25 | `employee` / `scheduler` | **Calendar** → *▾ Minimise* the agenda, *Collapse all days*, **⤓ Add to Outlook (.ics)** | Collapsible agenda; calendar export |

**v3.2 performance-summary and mobility walkthrough**

| # | Log in as | Do this | What it shows |
|---|---|---|---|
| 26 | `manager` | *Synthetic Associate 017* → **Evidence** → log a note | Agent 7's context, the skills it shows and a tone check appear under the note |
| 27 | `manager` | Same profile → **Summary** → *1:1* → **✦ Summarise**; then *Annual review* → **Start the annual review from this summary** | Talking points for the 1:1; an annual summary citing evidence (click a ref chip), beside the fact sheet; the review form prefilled for `YYYY-FY` |
| 28 | `employee` | **Career & Mobility**: Data Analyst aspiration → gap table and learning curve → **Map gap training to my calendar**; upload `data/sample_resumes/finops_to_data_analyst.txt` under *CV skill evidence* | Cross-vertical gap analysis from assessed levels and CV claims; upskilling sessions on *My Calendar* |
| 29 | `employee` | **Internal jobs** → *Data Analyst* → **Apply internally** | Fit for the posting; the hiring contact and the manager are told |
| 30 | `manager` | **Talent Pool** → *Data Analyst* → open *Synthetic Associate 025* → **Shortlist** | Applicants and aspirants, own team first; talent card with CV evidence |
| 31 | `leader` | **Talent Pool** → tick *Include potential fits* | *Synthetic Associate 030* appears from recorded skills, without performance data |
| 32 | `hr` | **Internal Jobs** → *FP&A Analyst* → **Onboard as job profile** | JD-prefilled onboarding; the posting is mapped to the new profile |

Generating questions takes about 15–40 s. In the workplace simulation each assessor follow-up takes a few seconds, and the evidence report and plan take 20–40 s each.

---

## Architecture

```
Browser ──session + CSRF──▶ Portal  (Talent360i_phase11C_fixed_remote_hybrid/app.py, port 8010)
                             │  RBAC for 6 roles, workflow state in SQLite, AI-notice banners
                             │  Bearer token (shared secret in both .env files)
                             ▼
                        Pipeline API  (app/main.py, port 8000, JSON only)
                             │  upload + input validation, rate limits, audit log
                             ▼
     agents/  A1 match (Chroma) → A2 generate + self-check → A3 score → A4 simulation + evidence → A5 plan + training outline
              v3: A6 performance insight · A7 evidence classifier · A4 code reviewer · A8 skill advisor + CV reader
                  · A9 performance summary
                             │  single LLM choke point: agents/llm_client.py
                             ▼
                        Groq (dev) or CIS / Azure AI Inference (approved provider)
```

Both processes listen on `127.0.0.1` only. `start.py` launches both and stops both.

| Path | What |
|---|---|
| `start.py` | One-command setup and launcher |
| `app/main.py` | Pipeline API (FastAPI) |
| `agents/` | The five agents, `llm_client.py`, shared `security.py` controls |
| `Talent360i_phase11C_fixed_remote_hybrid/` | Portal: routes (`app.py`), templates, services, workflow DB layer |
| `scripts/` | Database build and seed scripts (run by `start.py`) |
| `data/` | Synthetic Hackfest dataset + sample resumes |
| `Talent360i_phase11C_fixed_remote_hybrid/routes/` | v3 route modules: people & profiles, the employee's own page, job profiles, platform (notifications, coding, integrations, health) |
| `…/services/performance.py`, `evidence.py`, `development.py`, `probation.py`, `job_profiles.py`, `coding.py`, `engagement.py`, `lifecycle_store.py` | v3 lifecycle services and data access |
| `agents/agent6_performance.py`, `agent7_evidence.py`, `agent4_code_review.py` | v3 agents (insight + review drafts, evidence classifier, code reviewer) |
| `agents/agent8_skill_advisor.py`, `…/services/skill_advisor.py` | v3.1 Agent 8 (skill pitch for job profiles, new-skill recommendations) and its human review flow |
| `…/routes/org_admin.py`, `…/routes/hr.py` | Onboarding, verticals, skill catalogue; the HR workspace |
| `agents/agent9_performance_summary.py`, `…/services/performance_summary.py` | v3.2 Agent 9 and the fact sheet it is built on |
| `…/services/mobility.py`, `…/services/smartrecruiters.py`, `…/routes/career.py` | v3.2 aspirations, gap analysis, applications, talent pool; the SmartRecruiters adapter |
| `scripts/lifecycle_worker.py`, `seed_lifecycle.py`, `generate_scale_dataset.py`, `benchmark_scale.py` | Background worker, demo data, 20k/40k scale data and benchmark |
| `Dockerfile`, `docker-compose.yml`, `deploy/` | Container deployment and the scale-out guide |
| `tests/` | Security-control, workflow and v3 lifecycle tests |
| `SECURITY.md` | Guideline → control → test map, threat model, known limitations |

---

## Configuration

Everything lives in `.env` (project root) and `Talent360i_phase11C_fixed_remote_hybrid/.env`. Both
files are git-ignored; see the `.env.example` next to each one.

| Variable | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `groq` | `groq` or `cis`. Groq is refused when `TALENT360_ENV=production` |
| `GROQ_API_KEY` / `CIS_API_KEY` | – | The only value you must set yourself |
| `TALENT360_BACKEND_TOKEN` | generated | Must match in both files; `start.py` keeps them in sync |
| `TALENT360_DEBUG` | `true` | Set `false` for any shared deployment (turns on HSTS, Secure cookies, required secrets) |
| `TALENT360_API_PORT` / `TALENT360_PORTAL_PORT` | `8000` / `8010` | Set as environment variables before `python start.py` |
| `SMARTRECRUITERS_COMPANY_ID` (portal `.env`) | – | Connects internal jobs to SmartRecruiters; empty = demo postings. Optional `SMARTRECRUITERS_API_TOKEN`, `SMARTRECRUITERS_DESTINATION` |

## Tests and security scans

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q                                      # 185 tests, no network / LLM needed
bandit -r agents app Talent360i_phase11C_fixed_remote_hybrid scripts start.py -x tests -ll
pip-audit -r requirements.txt                                  # see SECURITY.md for the chromadb note
```

The same checks, plus a TruffleHog secret scan, run in `.github/workflows/security.yml`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `TypeError: unsupported operand type(s) for \|` on start | Your virtualenv uses Python < 3.12. Recreate it with Python 3.12 |
| `Port 8000 is already in use` | An old `uvicorn` is still running: stop it, or set `TALENT360_API_PORT` |
| Upload says questions could not be generated | Check the LLM key in `.env`. Groq `403 Access denied` means the network blocks Groq: use `LLM_PROVIDER=cis` on the NIQ network |
| CIS calls time out | The CIS gateway is only reachable on the NIQ network / VPN |
| First `--setup` stalls at `build_chroma.py` | It is downloading the ~80 MB embedding model (one time only) |
| Forgot a password | Look in `.env`, or set a new one and run `python start.py --reseed` |

Run everything through `python start.py`. If you start uvicorn by hand, the portal must be started
**from inside its folder** (`uvicorn app:app --port 8010`). From the project root, `app` resolves to
the API package instead.

## Known limitations

See `SECURITY.md` section 5. In short, the leader *movement over time* and *item analysis* screens
still show synthetic sample figures (there is no assessment history yet); every other leader and
manager screen uses live data. The career ladder is derived from the role catalogue (next grade up in
the same tower), not from an HR succession model, and a promotion approval is only recorded here.
