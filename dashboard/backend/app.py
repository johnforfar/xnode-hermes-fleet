#!/usr/bin/env python3
"""hermes-dashboard backend.

Trello-style multi-agent task board for the xnode-hermes-fleet.

Architecture:
  - Postgres holds projects, agents, tasks, task_messages
  - Worker containers (hermes-agent-*) connect via TCP from the host's
    vz-* bridge subnet (192.168.0.0/16). Auth is `trust` for that subnet
    and rejected from anywhere else (host firewall blocks 5432 publicly).
  - This service exposes:
      GET  /api/health           → status + counts
      GET  /api/agents           → list of agents
      POST /api/agents           → register/seed an agent (idempotent)
      GET  /api/projects         → list of projects
      POST /api/projects         → create a project
      GET  /api/tasks            → list tasks (filterable by status, project, agent)
      POST /api/tasks            → create a new task in a project
      GET  /api/tasks/<id>       → task detail with messages
      POST /api/tasks/<id>/move  → manually move task between columns
      GET  /api/events           → SSE stream of board changes (5s poll)
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from typing import Optional

import psycopg2
import psycopg2.extras
import requests as http
from flask import Flask, Response, jsonify, request, stream_with_context

DATABASE_URL = os.environ["DATABASE_URL"]
PORT = int(os.environ.get("PORT", "5000"))
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://hermes-ollama.local:11434")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "hermes3:3b")

# Some container DNS setups resolve `.local` but not the bare hostname (or
# vice versa). We try the configured URL first, then both alternates so the
# chat-with-agent feature is robust to either DNS quirk.
def _ollama_candidates(primary: str) -> list[str]:
    cands = [primary]
    if ".local:" in primary:
        cands.append(primary.replace(".local:", ":"))
    elif "hermes-ollama:" in primary:
        cands.append(primary.replace("hermes-ollama:", "hermes-ollama.local:"))
    seen = set(); out = []
    for c in cands:
        if c not in seen:
            seen.add(c); out.append(c)
    return out

OLLAMA_URLS = _ollama_candidates(OLLAMA_URL)

# Default agents seeded on startup. The role names match the container
# names (e.g. hermes-agent-pm → role "pm"). Adding a new role: add an
# entry here AND deploy a new agent container with the matching name.
#
# `tier` is used by the org chart view: 'lead' agents sit above 'team'
# agents in the hierarchy. The CEO is the top of the pyramid; the PM
# coordinates the team; coder/researcher/writer are individual contributors.
DEFAULT_AGENTS = [
    {
        "name": "ceo",
        "role": "Chief Executive Officer",
        "tier": "executive",
        "reports_to": None,
        "system_prompt": (
            "You are the CEO of an OpenxAI hardware launch initiative. "
            "Your job is to set strategy, make resource allocation decisions, "
            "and unblock the team. When the user asks you something, respond "
            "with vision and decisiveness — you delegate execution to your PM "
            "but you own outcomes. Keep responses short, executive-level. "
            "Focus on what matters for the launch: shipping on time, hardware "
            "quality, customer adoption, and sovereignty principles."
        ),
    },
    {
        "name": "pm",
        "role": "Project Manager",
        "tier": "lead",
        "reports_to": "ceo",
        "system_prompt": (
            "You are the project manager for the OpenxAI hardware launch. "
            "You receive high-level goals from the CEO and break them into "
            "concrete, single-step subtasks for the team (coder, researcher, "
            "writer). Output ONE subtask at a time as a short title and 1-3 "
            "sentence description. Don't try to do specialist work yourself — "
            "delegate. Track risks and dependencies."
        ),
    },
    {
        "name": "coder",
        "role": "Software Engineer",
        "tier": "team",
        "reports_to": "pm",
        "system_prompt": (
            "You are a senior software engineer on the OpenxAI hardware launch. "
            "You handle firmware, drivers, the on-device LLM runtime (ollama / "
            "vLLM), and cloud-side integrations. Produce working code with "
            "brief explanations. Use code blocks. Prefer simple, correct, "
            "shippable solutions over clever ones."
        ),
    },
    {
        "name": "researcher",
        "role": "Hardware Researcher",
        "tier": "team",
        "reports_to": "pm",
        "system_prompt": (
            "You are a hardware researcher on the OpenxAI hardware launch. "
            "You investigate component selection, supplier options, certification "
            "requirements (FCC, CE, RoHS), benchmark data for on-device "
            "inference, and competitive analysis. Produce structured "
            "bullet-point summaries. Be honest about uncertainty."
        ),
    },
    {
        "name": "writer",
        "role": "Technical Writer",
        "tier": "team",
        "reports_to": "pm",
        "system_prompt": (
            "You are a technical writer on the OpenxAI hardware launch. "
            "You produce launch announcements, product pages, documentation, "
            "spec sheets, FAQs, and developer onboarding guides. Match the "
            "tone of the existing OpenxAI brand: technical, sovereign, "
            "permissionless. Keep paragraphs short. Avoid marketing fluff."
        ),
    },
]


def log(msg: str) -> None:
    print(f"[hermes-dashboard] {msg}", flush=True)


def db_connect():
    return psycopg2.connect(DATABASE_URL)


def db_connect_dict():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def wait_for(check_fn, name: str, max_attempts: int = 60, delay: float = 2.0) -> None:
    for attempt in range(1, max_attempts + 1):
        try:
            check_fn()
            log(f"{name} is ready")
            return
        except Exception as e:
            log(f"waiting for {name} ({attempt}/{max_attempts}): {e}")
            time.sleep(delay)
    raise RuntimeError(f"{name} did not become ready in time")


def check_db() -> None:
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")


def ensure_schema() -> None:
    """Idempotent schema setup. Safe to call on every startup."""
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id          SERIAL PRIMARY KEY,
                name        TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agents (
                id              SERIAL PRIMARY KEY,
                name            TEXT NOT NULL UNIQUE,
                role            TEXT NOT NULL,
                system_prompt   TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'idle',
                current_task_id INTEGER,
                last_seen_at    TIMESTAMPTZ,
                -- Org chart hierarchy:
                --   tier: 'executive' (CEO) | 'lead' (PM) | 'team' (specialists)
                --   reports_to: name of the parent agent (null for CEO)
                tier            TEXT NOT NULL DEFAULT 'team',
                reports_to      TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id                SERIAL PRIMARY KEY,
                project_id        INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                parent_task_id    INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
                title             TEXT NOT NULL,
                description       TEXT NOT NULL DEFAULT '',
                -- 5-state lifecycle (matches OwnStartup design):
                --   backlog      → manual staging area, agents do NOT pick up
                --   todo         → agent will claim on next poll
                --   in_progress  → agent is currently working
                --   review       → completed but needs human approval
                --   done         → final
                --   failed       → error path
                status            TEXT NOT NULL DEFAULT 'todo',
                assigned_role     TEXT NOT NULL,
                assigned_agent_id INTEGER REFERENCES agents(id) ON DELETE SET NULL,
                output            TEXT,
                error             TEXT,
                created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                started_at        TIMESTAMPTZ,
                completed_at      TIMESTAMPTZ,
                -- v1.4: human scheduling fields
                due_date          TIMESTAMPTZ,
                assignees         TEXT[] NOT NULL DEFAULT '{}'
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS task_messages (
                id      SERIAL PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                role    TEXT NOT NULL,
                content TEXT NOT NULL,
                ts      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS tasks_status_idx ON tasks(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS tasks_role_idx ON tasks(assigned_role)")
        cur.execute("CREATE INDEX IF NOT EXISTS task_messages_task_idx ON task_messages(task_id)")
        # Add tier/reports_to columns if upgrading from an older schema
        cur.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS tier TEXT NOT NULL DEFAULT 'team'")
        cur.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS reports_to TEXT")
        cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS due_date TIMESTAMPTZ")
        cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS assignees TEXT[] NOT NULL DEFAULT '{}'")
        conn.commit()
    log("schema ensured")


def seed_agents() -> None:
    with db_connect() as conn, conn.cursor() as cur:
        for agent in DEFAULT_AGENTS:
            cur.execute(
                """
                INSERT INTO agents (name, role, system_prompt, tier, reports_to)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE
                SET role = EXCLUDED.role,
                    tier = EXCLUDED.tier,
                    reports_to = EXCLUDED.reports_to
                    -- NOTE: system_prompt is NOT updated on redeploy so
                    -- user-edited personas survive across deploys.
                """,
                (
                    agent["name"],
                    agent["role"],
                    agent["system_prompt"],
                    agent.get("tier", "team"),
                    agent.get("reports_to"),
                ),
            )
        conn.commit()
    log(f"seeded {len(DEFAULT_AGENTS)} default agents")


# Seed tasks sourced from ENGINEERING/Master Launch Plan Assiging Roles - Own1.xlsx.
# Departments mapped to our 5 agents: ceo (executive/finance/legal/PMO),
# pm (sales/partnerships/events), coder (web/devrel/technical), writer
# (brand/content/community/comms/customer success).
# Each tuple is (agent_role, title, description, status). The first ~46 entries
# are pre-distributed across done/review/in_progress/todo so the kanban opens
# with every column populated; the rest land in `backlog` for human triage.
LAUNCH_TASKS = [
    ("writer", "Brand strategy and message system", "Brand story, category narrative, customer positioning, key selling points, tagline system, message hierarchy — [Dept: Brand and Creative] — KPI: Final approved messaging pack — Real-world owners: Soman, Maria", "done"),
    ("writer", "Visual identity system", "Typography, color system, design language, render direction, layout logic, visual consistency rules — [Dept: Brand and Creative] — KPI: Final brand kit approved — Real-world owners: Mohit", "done"),
    ("writer", "Brand asset production", "Social templates, brochure style, presentation style, banners, launch visuals, reusable creative system — [Dept: Brand and Creative] — KPI: 100 percent of launch assets use one system — Real-world owners: Sheila, Mahrukh, Fisal, Maria", "done"),
    ("coder", "Landing page strategy and copy", "Page structure, conversion logic, copywriting, use case flow, objections handling, call to action logic — [Dept: Web and Funnel] — KPI: Final page copy and page structure approved", "done"),
    ("coder", "Landing page design and build", "UI design, responsive development, animations, forms, funnel sections, technical implementation — [Dept: Web and Funnel] — KPI: Site live and working on desktop and mobile — Real-world owners: Mohit", "review"),
    ("coder", "Reservation and payment infrastructure", "Deposit flow, checkout, payment processor, merchant setup, confirmation flow, reservation tracking — [Dept: Web and Funnel / Finance] — KPI: Deposits successfully processed", "review"),
    ("coder", "CRM and analytics setup", "Lead capture, segmentation, source tracking, dashboard, event tracking, conversion reporting — [Dept: Web and Funnel / Growth] — KPI: Dashboard live with daily reporting — Real-world owners: John", "review"),
    ("writer", "Product story content library", "Core articles, product explainers, day to day use cases, comparison pieces, founder narrative content — [Dept: Content and Media] — KPI: 10 plus core launch content pieces — Real-world owners: Sheila, Mahrukh, Fisal, Maria", "review"),
    ("writer", "Video content production", "Hero promo, teaser, launch cutdowns, social clips, demo edits, event recap assets — [Dept: Content and Media] — KPI: 10 official videos published — Real-world owners: Somni, Sheila, John, Mahrukh, Maria, Kentoro", "review"),
    ("writer", "Product visuals and collateral", "Product renders, product photography, brochure, spec sheets, comparison graphics, press images — [Dept: Content and Media / Design] — KPI: All key collateral finalized — Real-world owners: Sheila, Mahrukh, Fisal, Trubiks", "review"),
    ("coder", "Tutorial and education content", "Setup guides, onboarding guides, demo walkthroughs, XnodeOS and X Studio explainers, FAQ content — [Dept: Product and Technical Enablement] — KPI: 5 plus tutorials live — Real-world owners: Nelson, Sheila, John, Kentoro", "in_progress"),
    ("researcher", "Search footprint and discoverability", "Blog publishing, forum content, Reddit style explainers, YouTube support content, SEO pages — [Dept: Content and Growth] — KPI: 15 plus indexed launch support pieces — Real-world owners: Sheila, John, Maria", "in_progress"),
    ("writer", "Community growth engine", "Telegram, Discord, Xnode Circle, audience activation, migration of existing communities, new member onboarding — [Dept: Community and Growth] — KPI: 20,000 community size target — Real-world owners: Nelson, Zeus, Soman", "in_progress"),
    ("writer", "Referral and ambassador program", "Referral system, incentives, ambassador tiers, mission logic, share mechanics, tracking — [Dept: Community and Growth] — KPI: 3,000 plus referral actions — Real-world owners: Nelson, Zeus, Mirza, Soman", "in_progress"),
    ("writer", "UGC and community mission program", "Launch missions, engagement challenges, meme tasks, review prompts, community content submissions — [Dept: Community and Growth] — KPI: 50 UGC pieces and 500 micro UGC — Real-world owners: Paulius, Zeus, Soman", "in_progress"),
    ("writer", "Email and lifecycle campaigns", "Lead nurture, launch countdown, reservation reminders, follow ups, segmentation campaigns — [Dept: Community and Growth / CRM] — KPI: 30 percent open rate target — Real-world owners: Sheila, Paulius", "in_progress"),
    ("writer", "KOL strategy and onboarding", "KOL segmentation, outreach, negotiation, onboarding, creative alignment, scheduling — [Dept: KOL and Communications] — KPI: 50 KOLs onboarded — Real-world owners: Soman, Paulius, Kentoro, Vasco, Jarkko, Maria", "todo"),
    ("writer", "Influencer content execution", "Creator briefs, content approvals, posting calendar, review coordination, launch day publishing — [Dept: KOL and Communications] — KPI: 25 influencers posting — Real-world owners: Nelson, Sheila, Soman, Kentoro", "todo"),
    ("writer", "PR and media relations", "Press release, media list, journalist outreach, press kit, spokesperson notes, follow up — [Dept: PR and Communications] — KPI: 5 major PR mentions — Real-world owners: Soman, Paulius, Sheila, Maria", "todo"),
    ("writer", "Founder communications and thought leadership", "Founder interviews, podcasts, opinion content, keynote narrative, public facing talking points — [Dept: PR and Communications] — KPI: 5 podcasts or live streams — Real-world owners: Paulius", "todo"),
    ("pm", "Distributor sales package", "Distributor deck, pricing sheet, margin logic, reseller FAQ, bulk terms, outreach materials — [Dept: Sales and Partnerships] — KPI: Full distributor pack approved", "todo"),
    ("pm", "Distributor and reseller pipeline", "Lead list building, outreach, demos, follow up, deal progression, relationship management — [Dept: Sales and Partnerships] — KPI: 10 strong distributors in pipeline — Real-world owners: Paulius", "todo"),
    ("pm", "Strategic partnership development", "AI ecosystem partnerships, co marketing partnerships, distribution partners, ecosystem allies — [Dept: Sales and Partnerships] — KPI: 5 strategic partnerships — Real-world owners: Soman, Paulius", "todo"),
    ("pm", "Bulk order conversion", "Negotiation, LOIs, reseller commitment, territory conversations, commercial closing support — [Dept: Sales and Partnerships] — KPI: 10 LOIs or serious commitments", "todo"),
    ("pm", "Builder and developer acquisition", "Builder outreach, partner signups, product demos for builders, startup ecosystem onboarding — [Dept: Partnerships / Growth] — KPI: 1,000 builder commitments", "todo"),
    ("pm", "Launch event strategy and production", "Event concept, stage story, run of show, guest journey, event design, venue coordination — [Dept: Event and Launch Ops] — KPI: Event delivered with no major failures", "todo"),
    ("pm", "Keynote and demo development", "Script, slide flow, product reveal story, demo narrative, speaking notes, rehearsal support — [Dept: Event and Launch Ops / Executive] — KPI: Full keynote deck and script approved", "todo"),
    ("pm", "Livestream and content capture", "Stream setup, filming, photography, clipping, same day edits, content distribution from event — [Dept: Event and Launch Ops / Media] — KPI: Livestream successful and clip pack delivered — Real-world owners: Sheila, Mahrukh, Fisal", "todo"),
    ("pm", "Launch day conversion operations", "Site updates, launch emails, KOL coordination, PR timing, CTA synchronization, urgency triggers — [Dept: Event Ops / Growth / Web] — KPI: Reservation spike on launch day", "todo"),
    ("writer", "Post launch 30 day growth sprint", "Daily campaigns, content waves, follow ups, retargeting, referral pushes, momentum management — [Dept: Growth / Community / Media] — KPI: 30 day campaign calendar executed — Real-world owners: Sheila, Mahrukh, Fisal", "todo"),
    ("ceo", "Investor narrative and fundraising materials", "Seed deck, launch narrative, metrics sheet, market opportunity framing, investor FAQ — [Dept: Investor Relations] — KPI: Final investor pack ready", "todo"),
    ("ceo", "Investor outreach and meeting pipeline", "Warm outreach, meeting scheduling, strategic investor targeting, post launch follow up — [Dept: Investor Relations] — KPI: $1M seed conversations active", "todo"),
    ("ceo", "Financial and commercial readiness", "Bank accounts, payment operations, cash collection logic, reporting readiness, order reconciliation — [Dept: Finance and Compliance] — KPI: All payment and reporting workflows operational", "todo"),
    ("ceo", "Legal and compliance readiness", "Contracts, reservation terms, distributor agreements, KOL agreements, disclaimers, policy review — [Dept: Legal and Compliance] — KPI: All launch critical docs approved", "todo"),
    ("coder", "Product demo readiness", "Demo units, scripts, use cases, benchmark material, app showcase, event and partner demos — [Dept: Product and Technical Enablement] — KPI: 100 percent demo readiness — Real-world owners: Sam", "todo"),
    ("coder", "Technical product documentation", "Specs, compatibility notes, onboarding material, architecture explanations, buyer facing clarity — [Dept: Product and Technical Enablement] — KPI: Documentation pack completed — Real-world owners: Sam", "todo"),
    ("writer", "XnodeOS and X Studio positioning", "Clarify product architecture and relationship between device, OS, apps, and broader ecosystem — [Dept: Product and Messaging] — KPI: One approved product architecture explainer — Real-world owners: Sam", "todo"),
    ("writer", "Customer support readiness", "Inquiry handling, FAQ answers, escalation paths, response templates, support workflow — [Dept: Customer Success] — KPI: Under 12 hour response time — Real-world owners: Kentoro, Chris", "todo"),
    ("researcher", "Buyer trust and social proof systems", "Testimonials, partner logos, early supporter quotes, case studies, proof blocks on funnel — [Dept: Growth / Sales / Content] — KPI: Social proof modules live — Real-world owners: Sheila, Mahrukh, Fisal", "todo"),
    ("ceo", "PMO and launch governance", "Workstream management, owner tracking, meeting cadence, dependencies, risk register, reporting — [Dept: Executive and PMO] — KPI: Weekly launch dashboard live", "todo"),
    ("pm", "AI project management agent rollout", "Tool selection, workflow design, reporting logic, check in automation, team adoption — [Dept: PMO / Ops / Technical] — KPI: Agent pilot live and used daily — Real-world owners: Sam", "todo"),
    ("ceo", "Cross functional launch reporting", "KPI reviews, blockers, progress summaries, executive visibility, action tracking — [Dept: PMO / Executive] — KPI: Daily or weekly reporting rhythm active", "todo"),
    ("ceo", "Launch risk and issue management", "Identify risks, escalation rules, mitigation actions, launch readiness checks, contingency handling — [Dept: PMO / Executive / Ops] — KPI: Risk register maintained weekly", "todo"),
    ("ceo", "Internal team mobilization", "Role clarity, ownership assignment, working groups, accountability culture, sprint discipline — [Dept: Executive and PMO] — KPI: 100 percent critical workstreams have owners", "todo"),
    ("pm", "Distributor and investor follow up program", "Structured follow up after demos, after event, after outreach, and after launch day — [Dept: Sales / Investor Relations] — KPI: Follow up SLA under 48 hours", "todo"),
    ("writer", "Post purchase and preorder communications", "Buyer updates, expectation management, timeline communication, trust preservation after payment — [Dept: Customer Success / Growth] — KPI: Reservation refund complaints kept low", "todo"),
    ("researcher", "Market listening and narrative management", "Monitor reactions, objections, competitor comparisons, misinformation, sentiment — [Dept: PR / Growth / Executive] — KPI: Weekly narrative report — Real-world owners: Chris", "backlog"),
    ("coder", "Conversion optimization and funnel improvement", "A/B tests, page fixes, CTA changes, objection handling improvements, checkout optimization — [Dept: Web and Funnel / Growth] — KPI: 5 percent landing page conversion target", "backlog"),
    ("pm", "Performance marketing and paid distribution", "Paid traffic tests, retargeting, creative testing, funnel optimization, CAC tracking — [Dept: Growth and Performance] — KPI: Target CAC and conversion benchmarks", "backlog"),
    ("ceo", "Launch war room leadership", "Daily coordination across all departments during final run up and launch week — [Dept: Executive and PMO] — KPI: Daily blocker resolution and status closure", "backlog"),
    ("writer", "Ambassador activation and coordination", "Recruit ambassadors, define responsibilities, assign regional or audience focus, set reporting rhythm, track output — [Dept: Community and Growth] — KPI: Number of active ambassadors, weekly completion rate — Real-world owners: Nelson, Zeus, Ryan, Mirza, Soman", "backlog"),
    ("writer", "Ownership narrative amplification", "Push the core story across social channels and communities such as own your future, ownership computer, against rented dependency — [Dept: Community and Growth] — KPI: Number of ambassador posts, impressions, engagement rate — Real-world owners: Nelson, Soman, Kentoro", "backlog"),
    ("writer", "Referral and reservation driving", "Use referral links, personal outreach, private groups, and community channels to drive waitlist signups and deposits — [Dept: Community and Growth] — KPI: Leads generated, reservations attributed — Real-world owners: Soman, Paulius", "backlog"),
    ("writer", "Founder belief and personal story content", "Ambassadors create personal posts, videos, and threads explaining why they support the product and what ownership means to them — [Dept: Community and Growth] — KPI: Number of testimonial posts, saves, shares — Real-world owners: Somni, Zeus, Riccardo, Soman", "backlog"),
    ("writer", "UGC creation program", "Short videos, reaction clips, memes, screenshots, launch reactions, product wish content, user style storytelling — [Dept: Community and Growth] — KPI: UGC pieces created, engagement per piece — Real-world owners: Somni, Jarkko, Paulius, Kentoro, Chris, Riccardo", "backlog"),
    ("writer", "Launch mission execution", "Weekly or daily tasks for ambassadors including sharing posts, commenting, inviting friends, posting clips, joining spaces — [Dept: Community and Growth] — KPI: Mission completion rate, actions completed — Real-world owners: Somni, Kentoro", "backlog"),
    ("writer", "Live event and livestream amplification", "Ambassadors attend livestreams or events, post real time reactions, share clips, amplify keynote moments — [Dept: Community and Growth] — KPI: Live post count, livestream mentions, clip shares — Real-world owners: Somni, Melstein, Kentoro", "backlog"),
    ("writer", "Comment and reply support", "Ambassadors support official posts with comments, replies, answers, and public momentum — [Dept: Community and Growth] — KPI: Reply count, response speed, sentiment quality — Real-world owners: Somni, Chris, Jarkko, Alessio, Riccardo, Frifalin, Ryan, Mirza, Soman, Kentoro", "backlog"),
    ("writer", "Local community evangelism", "Share Xnode in founder groups, AI communities, student groups, startup circles, and local tech networks — [Dept: Community and Growth] — KPI: Number of communities reached, intros made — Real-world owners: Soman, Melstein, Paulius", "backlog"),
    ("researcher", "Objection and sentiment capture", "Ambassadors report back recurring objections, confusion points, competitor mentions, and positive hooks — [Dept: Community and Growth] — KPI: Weekly insight reports, objection log maintained — Real-world owners: Melstein, Paulius, Chris", "backlog"),
    ("writer", "Community education system", "Explain what Xnode is, what the category means, and how it differs from mini PCs, node boxes, and cloud dependence — [Dept: Community and Growth] — KPI: Educational posts published, reduction in repeated confusion — Real-world owners: Somni, Melstein", "backlog"),
    ("writer", "Ownership computer category education", "Repeated internal and public explanation of the new category and why it matters — [Dept: Community and Growth] — KPI: Community poll accuracy, fewer category related questions — Real-world owners: Somni", "backlog"),
    ("writer", "Rented dependency conversation leadership", "Start and guide discussions around cloud lock in, subscription captivity, privacy loss, and owning your machine — [Dept: Community and Growth] — KPI: Discussion threads started, engagement quality — Real-world owners: Melstein", "backlog"),
    ("writer", "Buyer identity reinforcement", "Position owners as serious, independent, future ready, and ahead of the curve — [Dept: Community and Growth] — KPI: Sentiment lift, number of identity driven posts — Real-world owners: Paulius", "backlog"),
    ("writer", "Founding Owner culture building", "Create prestige around early adopters through status, recognition, special roles, and early access positioning — [Dept: Community and Growth] — KPI: Number of Founding Owner signups, community participation — Real-world owners: Ryan, Akshay, Paulius", "backlog"),
    ("writer", "Early adopter recognition program", "Spotlight first builders, first supporters, first reservers, top contributors, and early believers — [Dept: Community and Growth] — KPI: Spotlights published, engagement on spotlight content — Real-world owners: Paulius, Sheila", "backlog"),
    ("writer", "Community onboarding and activation", "Welcome new members, explain channels, share key links, direct people to reservation and information flows — [Dept: Community and Growth] — KPI: Activation rate, welcome flow completion — Real-world owners: Step, Alessio, Riccardo, Kentoro", "backlog"),
    ("researcher", "Testimonial and quote collection", "Gather community reactions, attendee feedback, supporter quotes, and short endorsements for reuse — [Dept: Community and Growth] — KPI: Testimonials collected, approved quotes ready — Real-world owners: Melstein, Paulius, Zeus", "backlog"),
    ("researcher", "Feedback loop to launch team", "Weekly reporting from community on what people understand, misunderstand, want, or resist — [Dept: Community and Growth] — KPI: Weekly report delivered, action items created", "backlog"),
    ("writer", "Community challenge and contest program", "Memes, explainers, share to win, post your setup vision, builder challenge, launch countdown tasks — [Dept: Community and Growth] — KPI: Participation count, submissions received — Real-world owners: Somni", "backlog"),
    ("writer", "Community reactivation program", "Re engage old Telegram, Discord, email, and past supporter audiences into the launch cycle — [Dept: Community and Growth] — KPI: Reactivated members, click through rate — Real-world owners: Nelson, Zeus", "backlog"),
    ("coder", "DevRel builder onboarding program", "Design a simple path for developers to understand, test, and build with the device and stack — [Dept: DevRel] — KPI: Builder signups, onboarding completions — Real-world owners: John", "backlog"),
    ("coder", "Ownership computer technical translation", "Explain the new category in practical technical language for developers and advanced users — [Dept: DevRel] — KPI: Technical explainer published, views or downloads — Real-world owners: John", "backlog"),
    ("coder", "XnodeOS and Studio positioning", "Clarify how hardware, OS, apps, agents, and broader ecosystem fit together — [Dept: DevRel] — KPI: Approved architecture explainer, lower confusion in support — Real-world owners: John", "backlog"),
    ("coder", "One machine many roles showcase", "Demonstrate how the device can function as AI machine, startup machine, app server, creative workstation, automation base — [Dept: DevRel] — KPI: Number of demos, demo engagement — Real-world owners: John", "backlog"),
    ("coder", "Local AI and private execution education", "Teach the advantages of local power, private execution, always ready workflows, and owned infrastructure — [Dept: DevRel] — KPI: Tutorials or explainers published — Real-world owners: John", "backlog"),
    ("coder", "Technical demo and walkthrough series", "Live or recorded demos covering setup, workflows, app deployment, agents, automation, and real use cases — [Dept: DevRel] — KPI: Demo views, attendees, completion rate — Real-world owners: Akshay, John", "backlog"),
    ("coder", "Technical documentation pack", "Quick starts, setup guides, architecture notes, integration docs, troubleshooting, technical FAQ — [Dept: DevRel] — KPI: Docs published, usage or reads — Real-world owners: Sam, John", "backlog"),
    ("coder", "Use case and workflow library", "Publish sample use cases across AI, automation, apps, research, trading, content, and creator workflows — [Dept: DevRel] — KPI: Number of use cases published — Real-world owners: John", "backlog"),
    ("coder", "Hackathon and builder challenge program", "Create technical challenges, build campaigns, early deployment tasks, and demo competitions — [Dept: DevRel] — KPI: Participants, submissions, demos built — Real-world owners: John", "backlog"),
    ("coder", "Builder office hours and technical AMA", "Recurring live sessions for questions, support, and technical discovery — [Dept: DevRel] — KPI: Sessions run, attendance, follow up actions — Real-world owners: John", "backlog"),
    ("coder", "First 100 builders program", "Identify and support an early builder cohort with recognition, access, and structured onboarding — [Dept: DevRel] — KPI: Builder commitments, completed projects — Real-world owners: John", "backlog"),
    ("coder", "Sample app and integration showcases", "Show real apps, workflows, and partner software running on the device — [Dept: DevRel] — KPI: Number of showcase examples — Real-world owners: Sam, John", "backlog"),
    ("coder", "Technical partner enablement", "Help partners demonstrate their apps, agents, or tools on the device with good documentation and demo support — [Dept: DevRel] — KPI: Supported partner demos, integration readiness — Real-world owners: John", "backlog"),
    ("coder", "Developer content distribution", "Technical threads, demos, GitHub style content, videos, explainers, and builder oriented publishing — [Dept: DevRel] — KPI: Number of technical pieces published — Real-world owners: John", "backlog"),
    ("researcher", "Developer feedback and product insights", "Collect requests, blockers, use cases, friction points, and product feedback from builders — [Dept: DevRel] — KPI: Weekly feedback summaries, issues logged — Real-world owners: John", "backlog"),
    ("researcher", "Partnership pipeline building", "Build target list across AI, infra, hardware, media, education, Web3, startup ecosystems, and distribution channels — [Dept: Partnerships] — KPI: Number of qualified targets added — Real-world owners: Soman", "backlog"),
    ("researcher", "Partner segmentation and prioritization", "Categorize partners into co marketing, technical, distribution, media, education, ecosystem, and regional groups — [Dept: Partnerships] — KPI: Segmented pipeline completed — Real-world owners: Soman", "backlog"),
    ("pm", "Partnership pitch and collateral", "Prepare partner deck, value proposition, talking points, benefit summary, and next step materials — [Dept: Partnerships] — KPI: Final partner pack approved — Real-world owners: Sheila, Mahrukh, Fisal", "backlog"),
    ("pm", "Category aligned partnership strategy", "Identify and prioritize partners that reinforce ownership, private execution, and future ready computing — [Dept: Partnerships] — KPI: Number of category aligned partners engaged — Real-world owners: Paulius", "backlog"),
    ("pm", "Co marketing partner outreach", "Reach out to aligned communities, tools, brands, and ecosystems for joint campaigns and launch support — [Dept: Partnerships] — KPI: Outreach volume, meetings booked — Real-world owners: Nelson, Melstein", "backlog"),
    ("pm", "Launch amplification partner program", "Secure partners who will post, feature, mention, or support the launch window — [Dept: Partnerships] — KPI: Number of active launch partners", "backlog"),
    ("pm", "Technical showcase partnerships", "Work with tools, agent frameworks, software teams, and infra players to show practical compatibility — [Dept: Partnerships] — KPI: Showcase partners confirmed", "backlog"),
    ("pm", "Distribution and reseller partner development", "Identify and engage boutique distributors, resellers, startup channels, and commercial sales allies — [Dept: Partnerships] — KPI: Distributor meetings, LOIs, pipeline value", "backlog"),
    ("pm", "Education and university partnerships", "Approach universities, AI clubs, accelerators, builder communities, and startup programs for demos or pilots — [Dept: Partnerships] — KPI: Meetings, pilots, event invites — Real-world owners: Soman", "backlog"),
    ("pm", "Builder ecosystem partnerships", "Work with dev tools, hackathons, frameworks, communities, and startup networks to attract builders — [Dept: Partnerships] — KPI: Number of active ecosystem partners", "backlog"),
    ("pm", "Podcast and media collaboration partnerships", "Secure podcasts, newsletters, communities, and media channels for discussions and features — [Dept: Partnerships] — KPI: Podcasts booked, newsletter placements", "backlog"),
    ("pm", "Regional community partner program", "Build local market champions and community allies in target geographies — [Dept: Partnerships] — KPI: Regional partners activated — Real-world owners: Soman", "backlog"),
    ("pm", "Founding launch partner program", "Create a special tier for early ecosystem allies with visibility, recognition, and launch involvement — [Dept: Partnerships] — KPI: Founding partners signed", "backlog"),
    ("pm", "Bundle and offer partnership program", "Create software bundles, credits, perks, certificates, or launch bonuses through partners — [Dept: Partnerships] — KPI: Number of bundle offers live", "backlog"),
    ("pm", "Partner onboarding system", "Give each new partner a clear process, assets, messaging, timelines, and operational next steps — [Dept: Partnerships] — KPI: Time from agreement to activation", "backlog"),
    ("researcher", "Partnership CRM and reporting", "Track stage, owner, next step, probability, and strategic value of each relationship — [Dept: Partnerships] — KPI: Weekly partnership report", "backlog"),
    ("pm", "Joint campaign execution", "Run AMAs, Spaces, webinars, demo days, co branded content, and shared community pushes — [Dept: Partnerships] — KPI: Number of campaigns executed — Real-world owners: Chris", "backlog"),
    ("pm", "Post launch partner activation", "Convert launch momentum into longer term case studies, demos, integrations, and recurring campaigns — [Dept: Partnerships] — KPI: Post launch activities completed", "backlog"),
    ("researcher", "Partner feedback and strategic insight", "Capture what partners see in the market, what resonates, and where demand or confusion exists — [Dept: Partnerships] — KPI: Monthly partner feedback summary", "backlog"),
    ("writer", "Community and partner narrative training", "Train ambassadors, advocates, DevRel, and partners to explain the product with consistent language — [Dept: Brand and Growth] — KPI: Training completed, messaging accuracy checks — Real-world owners: Paulius, Chris, Melstein", "backlog"),
    ("writer", "Founder manifesto and movement content distribution", "Distribute the deeper story about owned versus rented computing through community, DevRel, and partners — [Dept: Brand and Growth] — KPI: Manifesto placements, shares, engagement", "backlog"),
    ("writer", "Founding Owner and ritual activation support", "Support launch rituals such as serialized units, early owner status, first builder wall, and owner recognition — [Dept: Community and Partnerships] — KPI: Ritual participation, owner signups", "backlog"),
    ("writer", "Community proof and case study sourcing", "Source examples, quotes, workflows, and stories from community members and partners for reuse in launch assets — [Dept: Community and Partnerships] — KPI: Case studies sourced, approved proof assets", "backlog"),
    ("writer", "Category creation strategy", "Define and operationalize the category Ownership Computer across all assets, decks, scripts, and public language — [Dept: Brand and Positioning] — KPI: Category line used consistently across 100 percent of launch assets", "backlog"),
    ("writer", "Brand thesis rollout", "Turn the next era of computing should be owned, not rented into a usable launch message system — [Dept: Brand and Positioning] — KPI: Master messaging pack approved — Real-world owners: Chris, Maria", "backlog"),
    ("writer", "Enemy framing system", "Build messaging around rented dependency, cloud lock in, subscription captivity, and fragmented tools — [Dept: Brand and Positioning] — KPI: 5 to 10 approved objection and contrast lines — Real-world owners: Maria", "backlog"),
    ("writer", "Human truth and emotional hook", "Translate I do not want to rent my future, I want to own it into page copy, scripts, ads, and community language — [Dept: Brand and Positioning] — KPI: Emotional hook appears in hero copy, video, and keynote — Real-world owners: Somni, Maria", "backlog"),
    ("writer", "Product role clarification", "Clarify how the machine is positioned as AI machine, startup machine, private server, creative workstation, and personal infrastructure base — [Dept: Brand and Positioning] — KPI: 1 approved use case architecture", "backlog"),
    ("writer", "Buyer identity positioning", "Frame ownership as a signal of being ahead, serious, empowered, and independent — [Dept: Brand and Positioning] — KPI: Identity language integrated into launch copy — Real-world owners: Chris", "backlog"),
    ("writer", "Naming exploration and decision support", "Run internal comparison of Xnode, NODA, AUREN, and any shortlisted names — [Dept: Brand and Positioning] — KPI: Final shortlist and decision memo", "backlog"),
    ("writer", "Brand architecture design", "Define relationship between Openmesh, Xnode, hardware brand, OS, Studio, and network layer — [Dept: Brand and Positioning] — KPI: Final brand architecture chart", "backlog"),
    ("writer", "Tagline and verbal identity system", "Finalize taglines, vocabulary, banned words, tone rules, and voice principles — [Dept: Brand and Positioning] — KPI: Verbal identity guide approved — Real-world owners: Chris", "backlog"),
    ("writer", "Visual DNA translation", "Convert design principles like silent power, sculptural object, premium neutrals, and activation rituals into usable creative guidelines — [Dept: Brand and Creative] — KPI: Visual reference system approved", "backlog"),
    ("writer", "Product doctrine alignment", "Ensure product demos, setup flow, and industrial storytelling reflect control, readiness, and long term ownership — [Dept: Brand and Product] — KPI: Brand doctrine checklist completed — Real-world owners: Soman", "backlog"),
    ("writer", "Launch ritual design", "Founding Owner system, numbered units, first boot ritual, public early adopter wall, and early owner status system — [Dept: Brand and Community] — KPI: 2 to 4 rituals implemented", "backlog"),
    ("writer", "Founder manifesto development", "Create a strong founder narrative around ownership versus renting your digital life — [Dept: Brand and Communications] — KPI: Manifesto published or used in keynote — Real-world owners: Maria", "backlog"),
    ("writer", "Brand narrative training", "Train internal team, ambassadors, advocates, and DevRel on how to explain the product correctly — [Dept: Brand and Communications] — KPI: 100 percent of public facing team trained", "backlog"),
    ("writer", "Founding Owner campaign support", "Promote early reservation identity, early owner perks, and serialized or limited first units — [Dept: Community and Growth] — KPI: Referrals and reservations from ambassador links", "backlog"),
    ("writer", "Personal why I support this campaign", "Each ambassador explains why owning the machine matters to them — [Dept: Community and Growth] — KPI: Testimonial or story posts created — Real-world owners: Jarkko", "backlog"),
    ("writer", "Category education posting", "Help public understand this is not a mini PC, not a crypto node, but a new ownership computer category — [Dept: Community and Growth] — KPI: Number of educational posts or threads — Real-world owners: Somni, Jarkko", "backlog"),
    ("writer", "Social proof creation", "Reaction videos, clips, screenshots, comments, short explainers, and memes tied to ownership and future control — [Dept: Community and Growth] — KPI: UGC count and engagement — Real-world owners: Somni, Paulius, Kentoro, Jarkko, trubiks, Akshay, Vasco, Chris, Riccardo, Frifalin, Mirza", "backlog"),
    ("writer", "Launch ritual participation", "Drive countdowns, Founding Owner identity, owner badge culture, and early owner pride — [Dept: Community and Growth] — KPI: Mission completion and participation rate — Real-world owners: Paulius, Chris", "backlog"),
    ("writer", "Local evangelism around ownership theme", "Introduce product in founder, AI, builder, and startup communities as a new category — [Dept: Community and Growth] — KPI: Number of groups or communities reached", "backlog"),
    ("writer", "Objection surfacing", "Capture where people resist the narrative or misunderstand the offer — [Dept: Community and Growth] — KPI: Weekly objections report — Real-world owners: Chris", "backlog"),
    ("writer", "Category explanation inside community", "Repeatedly explain what Ownership Computer means and why it matters — [Dept: Community and Growth] — KPI: Reduction in repeated confusion — Real-world owners: Somni", "backlog"),
    ("writer", "Rented dependency discussion leadership", "Start conversations about why people are tired of renting software, cloud, and infrastructure — [Dept: Community and Growth] — KPI: Number of meaningful discussion threads — Real-world owners: Somni", "backlog"),
    ("researcher", "Community proof collection", "Gather stories, reactions, practical use cases, questions, and trust signals from the community — [Dept: Community and Growth] — KPI: Testimonials and quotes collected — Real-world owners: Somni", "backlog"),
    ("writer", "Educational campaign support", "Support mini explainers around ownership, private execution, local AI, app deployment, and future readiness — [Dept: Community and Growth] — KPI: Educational posts and saves — Real-world owners: Ryan", "backlog"),
    ("writer", "Ritual and status activation", "Help run early adopter wall, owner status programs, and first builder recognition — [Dept: Community and Growth] — KPI: Participation in community rituals — Real-world owners: Paulius, Chris", "backlog"),
    ("researcher", "Feedback and narrative reporting", "Summarize how the community responds to the brand thesis, category, and naming — [Dept: Community and Growth] — KPI: Weekly summary submitted — Real-world owners: Paulius, Chris", "backlog"),
    ("coder", "Local control and private execution education", "Explain why local control, private execution, and always ready computing matter — [Dept: DevRel] — KPI: Technical content published — Real-world owners: John", "backlog"),
    ("coder", "XnodeOS and Studio developer positioning", "Clarify how the OS, app layer, and hardware work together for builders — [Dept: DevRel] — KPI: Stack explainer approved — Real-world owners: Sam", "backlog"),
    ("coder", "Build on your own infrastructure campaign", "Encourage builders to deploy on hardware they control rather than rented tools only — [Dept: DevRel] — KPI: Builder signups or challenge entries — Real-world owners: John", "backlog"),
    ("coder", "Founder and builder office hours", "Run sessions around what people can actually build or run on the device — [Dept: DevRel] — KPI: Session count and attendance", "backlog"),
    ("coder", "Technical use case library", "Publish use cases across agents, apps, automation, local inference, and workflow execution — [Dept: DevRel] — KPI: Number of technical use cases published", "backlog"),
    ("coder", "Integration readiness with partners", "Help technical partners show how their app or agent runs on the device — [Dept: DevRel] — KPI: Number of supported partner demos", "backlog"),
    ("coder", "Feedback loop from builders", "Capture requests, confusion, blockers, desired examples, and deployment pain points — [Dept: DevRel] — KPI: Weekly DevRel feedback report — Real-world owners: John", "backlog"),
    ("pm", "Category aligned partner mapping", "Identify partners that strengthen the ownership narrative, not just random logos — [Dept: Partnerships] — KPI: Qualified partner target list", "backlog"),
    ("pm", "Co marketing around ownership", "Create joint campaigns around owning infrastructure, private execution, and future ready workflows — [Dept: Partnerships] — KPI: Number of co marketing campaigns — Real-world owners: Soman", "backlog"),
    ("pm", "Education partnerships", "Work with universities, accelerator programs, founder communities, AI collectives, and bootcamps — [Dept: Partnerships] — KPI: Meetings, pilots, or activations — Real-world owners: Nelson, Soman", "backlog"),
    ("pm", "Media narrative partnerships", "Partner with newsletters, podcasts, communities, and thought leaders around ownership versus renting — [Dept: Partnerships] — KPI: Number of media collaborations — Real-world owners: Paulius, Soman", "backlog"),
    ("pm", "Ritual based partnership offers", "Bundle partner software, credits, certificates, launch perks, or early owner benefits — [Dept: Partnerships] — KPI: Number of bundle or perk offers", "backlog"),
    ("pm", "Regional champion partnerships", "Recruit local communities or operators to represent the product in specific markets — [Dept: Partnerships] — KPI: Regional activations or intros", "backlog"),
    ("pm", "Partnership story packaging", "Give partners messaging, visuals, talking points, and value propositions that match the ownership thesis — [Dept: Partnerships] — KPI: Partner toolkit delivered", "backlog"),
    ("pm", "Post launch ecosystem activation", "Turn launch partners into ongoing case studies, integrations, AMAs, and demos — [Dept: Partnerships] — KPI: Post launch partner activities executed — Real-world owners: Paulius, Chris", "backlog"),
    ("ceo", "Category and narrative enforcement", "Audit all launch assets to ensure they present the product as an Ownership Computer, not a mini PC — [Dept: Brand and PMO] — KPI: Asset audit completed", "backlog"),
    ("ceo", "Naming and transition readiness", "If rename is explored, prepare naming test, legal screening, domain review, visual mockups, and transition plan — [Dept: Brand and Strategy] — KPI: Rename decision package completed", "backlog"),
    ("writer", "Owner identity program", "Design Founding Owner, Genesis Unit, or first 1000 owners identity system — [Dept: Brand and Community] — KPI: Program launched — Real-world owners: Paulius, Chris", "backlog"),
    ("writer", "First boot activation experience", "Create cinematic activation or welcome sequence tied to ownership and readiness — [Dept: Product and Brand] — KPI: Activation concept approved", "backlog"),
    ("pm", "Early adopter wall and proof system", "Public wall of builders or owners, spotlight series, and serialized first batch recognition — [Dept: Community and Brand] — KPI: Wall launched and populated", "backlog"),
    ("writer", "Anti commodity positioning pack", "Create comparison language against mini PCs, closed devices, and technical ugly node boxes — [Dept: Brand and Positioning] — KPI: Comparison toolkit completed — Real-world owners: Akshay", "backlog"),
    ("ceo", "Future household expansion narrative", "Start preparing how the story expands beyond builders into homes and small businesses — [Dept: Brand and Strategy] — KPI: Future audience messaging drafted — Real-world owners: Chris", "backlog"),
]


def seed_demo_project() -> None:
    """Seed the OpenxAI Hardware Launch (Own1) project from the Master Launch Plan.

    Idempotent on the project name — if the project already exists we leave
    it alone (so deploys don't duplicate or overwrite human edits).
    """
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM projects WHERE name = %s", ("OpenxAI Hardware Launch (Own1)",))
        if cur.fetchone():
            return
        cur.execute(
            "INSERT INTO projects (name, description) VALUES (%s, %s) RETURNING id",
            (
                "OpenxAI Hardware Launch (Own1)",
                "Master launch plan for the Own1 sovereign-AI hardware unit. "
                "Tasks are sourced from the Own1 master roles & tasks spreadsheet "
                "and re-assigned to the hermes agent fleet (ceo / pm / coder / "
                "researcher / writer). Real-world human owners are preserved "
                "in each task description for reference.",
            ),
        )
        project_id = cur.fetchone()[0]
        # Stagger due dates across the next ~70 days so the calendar
        # and gantt views have something interesting to render. The
        # offset is per-task, not per-status, so each agent gets a
        # mix of near-term and farther-out work.
        from datetime import timedelta
        base = datetime.utcnow()
        for i, (role, title, description, status) in enumerate(LAUNCH_TASKS):
            # Earlier statuses (done/review) get past or near dates;
            # backlog gets pushed out further.
            if status == "done":
                offset = -7 - (i % 14)
            elif status == "review":
                offset = -1 - (i % 5)
            elif status == "in_progress":
                offset = 2 + (i % 5)
            elif status == "todo":
                offset = 5 + (i % 21)
            else:  # backlog
                offset = 25 + (i % 45)
            due = base + timedelta(days=offset)
            cur.execute(
                """
                INSERT INTO tasks (project_id, title, description, assigned_role,
                                   status, due_date, assignees)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (project_id, title, description, role, status, due, [role]),
            )
        conn.commit()
    log(f"seeded Own1 project + {len(LAUNCH_TASKS)} launch-plan tasks")



# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.after_request
def add_cors_headers(resp):
    # Same-origin only in production, but be permissive while developing.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/api/health")
def health():
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM agents")
        agent_count = cur.fetchone()[0]
        cur.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")
        by_status = {row[0]: row[1] for row in cur.fetchall()}
    return jsonify(
        {
            "status": "ok",
            "service": "hermes-dashboard",
            "agents": agent_count,
            "tasks": sum(by_status.values()),
            "backlog": by_status.get("backlog", 0),
            "todo": by_status.get("todo", 0),
            "in_progress": by_status.get("in_progress", 0),
            "review": by_status.get("review", 0),
            "done": by_status.get("done", 0),
            "failed": by_status.get("failed", 0),
        }
    )


@app.route("/api/agents")
def list_agents():
    with db_connect_dict() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id, a.name, a.role, a.tier, a.reports_to, a.status,
                   a.current_task_id, a.last_seen_at,
                   t.title AS current_task_title,
                   (SELECT COUNT(*) FROM tasks WHERE assigned_role = a.name) AS total_tasks,
                   (SELECT COUNT(*) FROM tasks WHERE assigned_role = a.name AND status = 'done') AS done_tasks,
                   (SELECT COUNT(*) FROM tasks WHERE assigned_role = a.name AND status IN ('todo','in_progress','review')) AS open_tasks
            FROM agents a
            LEFT JOIN tasks t ON t.id = a.current_task_id
            ORDER BY
              CASE a.tier WHEN 'executive' THEN 0 WHEN 'lead' THEN 1 ELSE 2 END,
              a.id
            """
        )
        rows = cur.fetchall()
    for r in rows:
        if r.get("last_seen_at"):
            r["last_seen_at"] = r["last_seen_at"].isoformat()
    return jsonify(rows)


@app.route("/api/projects")
def list_projects():
    with db_connect_dict() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, description, created_at FROM projects ORDER BY id")
        rows = cur.fetchall()
    for r in rows:
        if r.get("created_at"):
            r["created_at"] = r["created_at"].isoformat()
    return jsonify(rows)


@app.route("/api/projects", methods=["POST"])
def create_project():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    description = (data.get("description") or "").strip()
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (name, description) VALUES (%s, %s) RETURNING id",
            (name, description),
        )
        project_id = cur.fetchone()[0]
        conn.commit()
    return jsonify({"id": project_id, "name": name})


@app.route("/api/tasks")
def list_tasks():
    status = request.args.get("status")
    project_id = request.args.get("project_id", type=int)
    role = request.args.get("role")

    sql = """
        SELECT t.id, t.project_id, t.title, t.description, t.status,
               t.assigned_role, t.assigned_agent_id, t.output, t.error,
               t.created_at, t.started_at, t.completed_at,
               t.due_date, t.assignees,
               a.name AS assigned_agent_name
        FROM tasks t
        LEFT JOIN agents a ON a.id = t.assigned_agent_id
        WHERE 1=1
    """
    params: list = []
    if status:
        sql += " AND t.status = %s"
        params.append(status)
    if project_id:
        sql += " AND t.project_id = %s"
        params.append(project_id)
    if role:
        sql += " AND t.assigned_role = %s"
        params.append(role)
    sql += " ORDER BY t.created_at DESC"

    with db_connect_dict() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    for r in rows:
        for ts_field in ("created_at", "started_at", "completed_at", "due_date"):
            if r.get(ts_field):
                r[ts_field] = r[ts_field].isoformat()
    return jsonify(rows)


@app.route("/api/tasks", methods=["POST"])
def create_task():
    data = request.get_json(force=True, silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    description = (data.get("description") or "").strip()
    role = (data.get("role") or "pm").strip()
    status = (data.get("status") or "todo").strip()
    if status not in ("backlog", "todo", "in_progress", "review", "done", "failed"):
        return jsonify({"error": "invalid status"}), 400
    project_id = data.get("project_id")
    if not project_id:
        # Auto-assign to the first project for convenience
        with db_connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM projects ORDER BY id LIMIT 1")
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "no project to assign to"}), 400
            project_id = row[0]
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tasks (project_id, title, description, assigned_role, status)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
            """,
            (project_id, title, description, role, status),
        )
        task_id = cur.fetchone()[0]
        conn.commit()
    return jsonify({"id": task_id, "title": title, "role": role, "status": status})


@app.route("/api/tasks/<int:task_id>")
def get_task(task_id):
    with db_connect_dict() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.*, a.name AS assigned_agent_name
            FROM tasks t LEFT JOIN agents a ON a.id = t.assigned_agent_id
            WHERE t.id = %s
            """,
            (task_id,),
        )
        task = cur.fetchone()
        if not task:
            return jsonify({"error": "not found"}), 404
        for ts_field in ("created_at", "started_at", "completed_at", "due_date"):
            if task.get(ts_field):
                task[ts_field] = task[ts_field].isoformat()
        # Children (sub-tasks)
        cur.execute(
            """
            SELECT id, title, status, assigned_role, due_date, assignees
            FROM tasks WHERE parent_task_id = %s ORDER BY created_at
            """,
            (task_id,),
        )
        children = []
        for c in cur.fetchall():
            if c.get("due_date"):
                c["due_date"] = c["due_date"].isoformat()
            children.append(c)
        task["children"] = children
        # Parent
        if task.get("parent_task_id"):
            cur.execute(
                "SELECT id, title, status FROM tasks WHERE id = %s",
                (task["parent_task_id"],),
            )
            task["parent"] = cur.fetchone()
        cur.execute(
            "SELECT id, role, content, ts FROM task_messages WHERE task_id = %s ORDER BY id",
            (task_id,),
        )
        messages = cur.fetchall()
        for m in messages:
            if m.get("ts"):
                m["ts"] = m["ts"].isoformat()
        task["messages"] = messages
    return jsonify(task)


@app.route("/api/tasks/<int:task_id>/move", methods=["POST"])
def move_task(task_id):
    data = request.get_json(force=True, silent=True) or {}
    new_status = data.get("status")
    if new_status not in ("backlog", "todo", "in_progress", "review", "done", "failed"):
        return jsonify({"error": "invalid status"}), 400
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE tasks SET status = %s WHERE id = %s",
            (new_status, task_id),
        )
        conn.commit()
    return jsonify({"id": task_id, "status": new_status})


@app.route("/api/tasks/<int:task_id>/update", methods=["POST"])
def update_task(task_id):
    """Partial update — only the fields present in the body are written.

    Accepts: title, description, due_date (ISO 8601 string or null),
    assignees (list of agent name strings), assigned_role (single role,
    kept in sync with the first assignee for the polling worker).
    """
    data = request.get_json(force=True, silent=True) or {}
    sets = []
    params: list = []
    if "title" in data:
        sets.append("title = %s")
        params.append((data["title"] or "").strip()[:300])
    if "description" in data:
        sets.append("description = %s")
        params.append((data["description"] or "").strip())
    if "due_date" in data:
        sets.append("due_date = %s")
        v = data["due_date"]
        params.append(v if v else None)
    if "assignees" in data:
        a = data["assignees"] or []
        if not isinstance(a, list):
            return jsonify({"error": "assignees must be a list"}), 400
        a = [str(x).strip() for x in a if str(x).strip()]
        sets.append("assignees = %s")
        params.append(a)
        # Keep assigned_role in sync with the first assignee so the
        # polling worker still claims the task.
        if a:
            sets.append("assigned_role = %s")
            params.append(a[0])
    if "assigned_role" in data and "assignees" not in data:
        sets.append("assigned_role = %s")
        params.append(data["assigned_role"])
    if not sets:
        return jsonify({"error": "no updatable fields provided"}), 400
    params.append(task_id)
    sql = f"UPDATE tasks SET {', '.join(sets)} WHERE id = %s"
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        conn.commit()
    return jsonify({"id": task_id, "updated": True})


@app.route("/api/tasks/<int:task_id>/subtask", methods=["POST"])
def create_subtask(task_id):
    """Spawn a child task under an existing one.

    Used for the CEO→PM→specialist delegation chain. The child inherits
    the parent's project_id and gets parent_task_id set so the modal
    can render the hierarchy. Status defaults to 'todo' so the assigned
    agent's worker picks it up immediately.
    """
    data = request.get_json(force=True, silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    role = (data.get("role") or "pm").strip()
    description = (data.get("description") or "").strip()
    status = (data.get("status") or "todo").strip()
    if status not in ("backlog", "todo", "in_progress", "review", "done", "failed"):
        return jsonify({"error": "invalid status"}), 400
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT project_id FROM tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "parent task not found"}), 404
        project_id = row[0]
        cur.execute(
            """
            INSERT INTO tasks (project_id, parent_task_id, title, description,
                               assigned_role, status, assignees)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (project_id, task_id, title, description, role, status, [role]),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
    return jsonify({"id": new_id, "parent_id": task_id, "title": title, "role": role})


@app.route("/api/agents/<name>", methods=["GET"])
def get_agent(name):
    with db_connect_dict() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, role, tier, reports_to, system_prompt, status FROM agents WHERE name = %s",
            (name,),
        )
        agent = cur.fetchone()
    if not agent:
        return jsonify({"error": "not found"}), 404
    return jsonify(agent)


@app.route("/api/agents/<name>", methods=["POST"])
def update_agent(name):
    """Edit an agent's persona — system prompt, role label, or tier.

    The next polling cycle of the worker container will pick up the
    new system prompt automatically (it re-reads on each task claim).
    """
    data = request.get_json(force=True, silent=True) or {}
    sets = []
    params: list = []
    if "system_prompt" in data:
        sp = (data["system_prompt"] or "").strip()
        if not sp:
            return jsonify({"error": "system_prompt cannot be empty"}), 400
        sets.append("system_prompt = %s")
        params.append(sp)
    if "role" in data:
        sets.append("role = %s")
        params.append((data["role"] or "").strip()[:120])
    if "tier" in data:
        if data["tier"] not in ("executive", "lead", "team"):
            return jsonify({"error": "tier must be executive|lead|team"}), 400
        sets.append("tier = %s")
        params.append(data["tier"])
    if "reports_to" in data:
        sets.append("reports_to = %s")
        params.append(data["reports_to"] or None)
    if not sets:
        return jsonify({"error": "no updatable fields"}), 400
    params.append(name)
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE agents SET {', '.join(sets)} WHERE name = %s", params)
        if cur.rowcount == 0:
            return jsonify({"error": "agent not found"}), 404
        conn.commit()
    return jsonify({"name": name, "updated": True})


@app.route("/api/agents/<name>/chat/stream", methods=["POST"])
def chat_with_agent(name):
    """Stream a direct chat with one agent.

    The user picks an agent (e.g. 'coder'), sends a message, and the
    backend opens a connection to ollama (using the agent's system
    prompt from the agents table), streams tokens back via SSE.

    This is the "talk to little team members" feature — same agent
    persona as the polling worker, but synchronous instead of going
    through the task queue.
    """
    data = request.get_json(force=True, silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "empty message"}), 400
    if len(message) > 4000:
        return jsonify({"error": "message too long (max 4000 chars)"}), 400

    # Look up the agent's persona
    with db_connect_dict() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, role, system_prompt FROM agents WHERE name = %s",
            (name,),
        )
        agent = cur.fetchone()
    if not agent:
        return jsonify({"error": f"agent '{name}' not found"}), 404

    system_prompt = agent["system_prompt"]
    full_prompt = (
        f"{system_prompt}\n\n"
        f"You are {agent['name']} ({agent['role']}). The user is talking to you "
        f"directly as a team member. Respond conversationally, in character.\n\n"
        f"USER: {message}\n\nYOUR RESPONSE:"
    )

    def open_ollama_stream():
        last_err = None
        for url in OLLAMA_URLS:
            try:
                r = http.post(
                    f"{url}/api/generate",
                    json={
                        "model": CHAT_MODEL,
                        "prompt": full_prompt,
                        "stream": True,
                        "options": {"temperature": 0.6, "num_predict": 300},
                    },
                    stream=True,
                    timeout=600,
                )
                r.raise_for_status()
                return r
            except Exception as e:
                last_err = e
                log(f"ollama fallback: {url} failed ({e}), trying next")
                continue
        raise last_err or RuntimeError("no ollama url succeeded")

    def generate():
        try:
            yield f"event: agent\ndata: {json.dumps({'name': agent['name'], 'role': agent['role']})}\n\n"
            with open_ollama_stream() as r:
                r.raise_for_status()
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    token = obj.get("response", "")
                    if token:
                        safe = token.replace("\\", "\\\\").replace("\n", "\\n")
                        yield f"event: token\ndata: {safe}\n\n"
                    if obj.get("done"):
                        yield "event: done\ndata: {}\n\n"
                        return
        except Exception as e:
            log(f"chat-with-agent error: {e}")
            err = json.dumps({"error": str(e)})
            yield f"event: error\ndata: {err}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/insights/algorithmic")
def insights_algo():
    """Pure algorithmic metrics for the Advanced Insights tab.

    Computes everything from the tasks + agents tables — no LLM call.
    Returns a JSON dict the frontend can render directly into cards/charts.
    """
    with db_connect_dict() as conn, conn.cursor() as cur:
        cur.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")
        by_status = {r["status"]: r["count"] for r in cur.fetchall()}

        cur.execute("SELECT assigned_role, COUNT(*) FROM tasks GROUP BY assigned_role")
        by_role = {r["assigned_role"]: r["count"] for r in cur.fetchall()}

        cur.execute(
            """
            SELECT assigned_role,
                   COUNT(*) FILTER (WHERE status = 'done') AS done,
                   COUNT(*) FILTER (WHERE status IN ('todo','in_progress','review')) AS open
            FROM tasks GROUP BY assigned_role
            """
        )
        per_agent_load = [dict(r) for r in cur.fetchall()]

        cur.execute(
            """
            SELECT EXTRACT(EPOCH FROM AVG(completed_at - started_at)) AS avg_seconds
            FROM tasks WHERE completed_at IS NOT NULL AND started_at IS NOT NULL
            """
        )
        row = cur.fetchone()
        avg_cycle_seconds = float(row["avg_seconds"] or 0)

        cur.execute(
            """
            SELECT EXTRACT(EPOCH FROM AVG(completed_at - created_at)) AS avg_seconds
            FROM tasks WHERE completed_at IS NOT NULL
            """
        )
        row = cur.fetchone()
        avg_lead_seconds = float(row["avg_seconds"] or 0)

        cur.execute(
            """
            SELECT COUNT(*) AS overdue FROM tasks
            WHERE due_date < NOW() AND status NOT IN ('done','failed')
            """
        )
        overdue = cur.fetchone()["overdue"]

        cur.execute(
            """
            SELECT COUNT(*) AS unassigned FROM tasks
            WHERE (assignees IS NULL OR cardinality(assignees) = 0)
              AND status NOT IN ('done','failed')
            """
        )
        unassigned = cur.fetchone()["unassigned"]

        cur.execute(
            """
            SELECT COUNT(*) AS no_due FROM tasks
            WHERE due_date IS NULL AND status NOT IN ('done','failed')
            """
        )
        no_due = cur.fetchone()["no_due"]

        cur.execute(
            """
            SELECT date_trunc('day', completed_at) AS day, COUNT(*) AS done
            FROM tasks
            WHERE completed_at IS NOT NULL
              AND completed_at > NOW() - INTERVAL '14 days'
            GROUP BY day ORDER BY day
            """
        )
        velocity = [
            {"day": r["day"].isoformat()[:10], "done": r["done"]}
            for r in cur.fetchall()
        ]

        cur.execute(
            """
            SELECT id, title, due_date, assigned_role, status FROM tasks
            WHERE due_date < NOW() AND status NOT IN ('done','failed')
            ORDER BY due_date ASC LIMIT 20
            """
        )
        overdue_tasks = []
        for r in cur.fetchall():
            r["due_date"] = r["due_date"].isoformat() if r.get("due_date") else None
            overdue_tasks.append(r)

    return jsonify(
        {
            "by_status": by_status,
            "by_role": by_role,
            "per_agent_load": per_agent_load,
            "avg_cycle_seconds": avg_cycle_seconds,
            "avg_lead_seconds": avg_lead_seconds,
            "overdue_count": overdue,
            "unassigned_count": unassigned,
            "no_due_date_count": no_due,
            "velocity_14d": velocity,
            "overdue_tasks": overdue_tasks,
        }
    )


@app.route("/api/insights/ai/stream", methods=["POST"])
def insights_ai():
    """LLM commentary on the current board state.

    Builds a structured context block from the algorithmic insights
    (so we don't need RAG — the full dataset summary fits trivially in
    one prompt) and streams hermes3:3b's analysis back via SSE.
    """
    # Re-use the algo insights to build a context block
    with app.test_request_context("/api/insights/algorithmic"):
        algo = insights_algo().get_json()

    ctx_lines = [
        "Current Hermes Fleet Board State:",
        f"- total tasks: {sum(algo['by_status'].values())}",
        "- by status: " + ", ".join(f"{k}={v}" for k, v in algo["by_status"].items()),
        "- by agent: " + ", ".join(f"{k}={v}" for k, v in algo["by_role"].items()),
        f"- overdue tasks (past due, not done): {algo['overdue_count']}",
        f"- tasks with no due date: {algo['no_due_date_count']}",
        f"- tasks with no assignee: {algo['unassigned_count']}",
        f"- avg lead time (created→done): {int(algo['avg_lead_seconds'] / 3600)} hours",
        f"- velocity (last 14 days): {sum(d['done'] for d in algo['velocity_14d'])} tasks completed",
    ]
    if algo["overdue_tasks"]:
        ctx_lines.append("- top overdue tasks:")
        for t in algo["overdue_tasks"][:5]:
            ctx_lines.append(f"    * #{t['id']} [{t['assigned_role']}] {t['title']}")

    context = "\n".join(ctx_lines)
    prompt = (
        "You are an experienced engineering chief of staff analyzing a "
        "multi-agent project board for the OpenxAI hardware launch. "
        "Read the state below and produce a SHORT executive briefing "
        "(under 200 words) covering: (1) what's going well, (2) the top "
        "3 risks or bottlenecks, (3) one concrete recommendation. Be "
        "direct, no fluff.\n\n"
        f"{context}\n\nYOUR BRIEFING:"
    )

    def open_stream():
        last_err = None
        for url in OLLAMA_URLS:
            try:
                r = http.post(
                    f"{url}/api/generate",
                    json={
                        "model": CHAT_MODEL,
                        "prompt": prompt,
                        "stream": True,
                        "options": {"temperature": 0.4, "num_predict": 400},
                    },
                    stream=True,
                    timeout=600,
                )
                r.raise_for_status()
                return r
            except Exception as e:
                last_err = e
                continue
        raise last_err or RuntimeError("ollama unreachable")

    def gen():
        try:
            yield f"event: context\ndata: {json.dumps({'lines': len(ctx_lines)})}\n\n"
            with open_stream() as r:
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    tok = obj.get("response", "")
                    if tok:
                        safe = tok.replace("\\", "\\\\").replace("\n", "\\n")
                        yield f"event: token\ndata: {safe}\n\n"
                    if obj.get("done"):
                        yield "event: done\ndata: {}\n\n"
                        return
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/insights/ai/ask", methods=["POST"])
def insights_ai_ask():
    """Ask hermes3 a free-form question grounded in current board state.

    Same context-block trick as the briefing endpoint — we serialise
    the algo insights into a small structured block and prepend it to
    the user's question. No RAG, just prompt stuffing on a small dataset.
    """
    data = request.get_json(force=True, silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question required"}), 400
    if len(question) > 1000:
        return jsonify({"error": "question too long"}), 400

    with app.test_request_context("/api/insights/algorithmic"):
        algo = insights_algo().get_json()

    # Pull the most useful task summaries (titles + status + role + due)
    # so the LLM can name specific tasks in its answer.
    with db_connect_dict() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, title, status, assigned_role, due_date
            FROM tasks
            ORDER BY
              CASE status
                WHEN 'in_progress' THEN 0
                WHEN 'review' THEN 1
                WHEN 'todo' THEN 2
                WHEN 'done' THEN 3
                ELSE 4
              END,
              due_date NULLS LAST
            LIMIT 60
            """
        )
        sample = cur.fetchall()

    ctx = [
        "BOARD STATE:",
        f"- {sum(algo['by_status'].values())} tasks total",
        "- by status: " + ", ".join(f"{k}={v}" for k, v in algo["by_status"].items()),
        "- by agent: " + ", ".join(f"{k}={v}" for k, v in algo["by_role"].items()),
        f"- overdue: {algo['overdue_count']} · unassigned: {algo['unassigned_count']} · no due date: {algo['no_due_date_count']}",
        f"- avg lead time: {int(algo['avg_lead_seconds']/3600)}h",
        f"- 14-day velocity: {sum(d['done'] for d in algo['velocity_14d'])} tasks completed",
        "",
        "RECENT TASKS (id · status · role · title):",
    ]
    for t in sample[:40]:
        due = ""
        if t.get("due_date"):
            due = " · due " + t["due_date"].strftime("%Y-%m-%d")
        ctx.append(f"  #{t['id']} [{t['status']}] [{t['assigned_role']}] {t['title']}{due}")

    prompt = (
        "You are an experienced engineering chief of staff. Answer the user's "
        "question using ONLY the board state below. Be specific — name task IDs "
        "and agents when relevant. Be direct and brief (under 220 words). If the "
        "answer isn't in the data, say so.\n\n"
        + "\n".join(ctx)
        + f"\n\nQUESTION: {question}\n\nANSWER:"
    )

    def open_stream():
        last_err = None
        for url in OLLAMA_URLS:
            try:
                r = http.post(
                    f"{url}/api/generate",
                    json={
                        "model": CHAT_MODEL,
                        "prompt": prompt,
                        "stream": True,
                        "options": {"temperature": 0.4, "num_predict": 450},
                    },
                    stream=True,
                    timeout=600,
                )
                r.raise_for_status()
                return r
            except Exception as e:
                last_err = e
                continue
        raise last_err or RuntimeError("ollama unreachable")

    def gen():
        try:
            with open_stream() as r:
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    tok = obj.get("response", "")
                    if tok:
                        safe = tok.replace("\\", "\\\\").replace("\n", "\\n")
                        yield f"event: token\ndata: {safe}\n\n"
                    if obj.get("done"):
                        yield "event: done\ndata: {}\n\n"
                        return
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/events")
def events():
    """SSE stream of board state. Frontend polls every 3s for live updates.

    Lightweight implementation: every 3 seconds we send the full task and
    agent counts. Frontend re-renders if anything changed. We avoid
    LISTEN/NOTIFY for v1 simplicity.
    """

    def gen():
        last_summary = None
        while True:
            try:
                with db_connect() as conn, conn.cursor() as cur:
                    cur.execute(
                        "SELECT status, COUNT(*) FROM tasks GROUP BY status"
                    )
                    counts = {row[0]: row[1] for row in cur.fetchall()}
                    cur.execute("SELECT name, status, current_task_id FROM agents")
                    agent_states = [
                        {"name": r[0], "status": r[1], "current_task_id": r[2]}
                        for r in cur.fetchall()
                    ]
                summary = {"counts": counts, "agents": agent_states}
                if summary != last_summary:
                    yield f"event: state\ndata: {json.dumps(summary)}\n\n"
                    last_summary = summary
                else:
                    yield ": heartbeat\n\n"  # SSE comment heartbeat
            except Exception as e:
                yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
            time.sleep(3)

    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def main() -> int:
    log("starting up")
    log(f"  database: {DATABASE_URL.split('@')[-1] if '@' in DATABASE_URL else 'local'}")
    wait_for(check_db, "postgres")
    ensure_schema()
    seed_agents()
    seed_demo_project()
    log(f"listening on {BIND_HOST}:{PORT}")
    app.run(host=BIND_HOST, port=PORT, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
