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
from flask import Flask, Response, jsonify, request, stream_with_context

DATABASE_URL = os.environ["DATABASE_URL"]
PORT = int(os.environ.get("PORT", "5000"))
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")

# Default agents seeded on startup. The role names match the container
# names (e.g. hermes-agent-pm → role "pm"). Adding a new role: add an
# entry here AND deploy a new agent container with the matching name.
DEFAULT_AGENTS = [
    {
        "name": "pm",
        "role": "Project Manager",
        "system_prompt": (
            "You are the project manager. You receive high-level goals from the user "
            "and break them down into concrete, single-step subtasks that can be "
            "assigned to specialist agents (coder, researcher, writer). "
            "Output ONE subtask at a time as a short title and 1-3 sentence description. "
            "Do NOT try to solve the task yourself — that's the specialist's job."
        ),
    },
    {
        "name": "coder",
        "role": "Software Engineer",
        "system_prompt": (
            "You are a senior software engineer. You receive concrete coding tasks and "
            "produce working code with brief explanations. Use code blocks. Be concise "
            "and pragmatic. Prefer the simplest correct solution."
        ),
    },
    {
        "name": "researcher",
        "role": "Researcher",
        "system_prompt": (
            "You are a researcher. You receive questions and produce structured "
            "answers based on your training data. Be honest about uncertainty. "
            "Cite sources when you can. Output bullet-point summaries."
        ),
    },
    {
        "name": "writer",
        "role": "Technical Writer",
        "system_prompt": (
            "You are a technical writer. You receive draft text or topics and produce "
            "clean, structured prose. Match the tone of the source material. Keep "
            "paragraphs short. Avoid filler."
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
                last_seen_at    TIMESTAMPTZ
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
                completed_at      TIMESTAMPTZ
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
        conn.commit()
    log("schema ensured")


def seed_agents() -> None:
    with db_connect() as conn, conn.cursor() as cur:
        for agent in DEFAULT_AGENTS:
            cur.execute(
                """
                INSERT INTO agents (name, role, system_prompt)
                VALUES (%s, %s, %s)
                ON CONFLICT (name) DO UPDATE
                SET role = EXCLUDED.role, system_prompt = EXCLUDED.system_prompt
                """,
                (agent["name"], agent["role"], agent["system_prompt"]),
            )
        conn.commit()
    log(f"seeded {len(DEFAULT_AGENTS)} default agents")


def seed_demo_project() -> None:
    """Insert a demo project + a couple of starter tasks if the db is empty."""
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM projects")
        if cur.fetchone()[0] > 0:
            return
        cur.execute(
            "INSERT INTO projects (name, description) VALUES (%s, %s) RETURNING id",
            (
                "Hello hermes-fleet",
                "A demo project to verify the agent fleet is working.",
            ),
        )
        project_id = cur.fetchone()[0]
        starter_tasks = [
            ("pm", "Plan the demo deploy", "Outline the steps to demo this fleet to a hackathon student in under 5 minutes."),
            ("coder", "Write a Python hello function", "Write a Python function `hello(name)` that returns a friendly greeting."),
            ("researcher", "Why sovereign Xnodes?", "Summarise in 3 bullets why a developer might prefer a sovereign Xnode over a cloud VM."),
            ("writer", "Draft a tweet", "Draft a 240-character tweet announcing this hermes-fleet demo."),
        ]
        for role, title, description in starter_tasks:
            cur.execute(
                """
                INSERT INTO tasks (project_id, title, description, assigned_role)
                VALUES (%s, %s, %s, %s)
                """,
                (project_id, title, description, role),
            )
        conn.commit()
    log("seeded demo project + 4 starter tasks")


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
            SELECT a.id, a.name, a.role, a.status, a.current_task_id, a.last_seen_at,
                   t.title AS current_task_title
            FROM agents a
            LEFT JOIN tasks t ON t.id = a.current_task_id
            ORDER BY a.id
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
        for ts_field in ("created_at", "started_at", "completed_at"):
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
        for ts_field in ("created_at", "started_at", "completed_at"):
            if task.get(ts_field):
                task[ts_field] = task[ts_field].isoformat()
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
