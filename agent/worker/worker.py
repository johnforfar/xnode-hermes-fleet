#!/usr/bin/env python3
"""hermes-fleet agent worker (v1).

A tiny polling loop that:
  1. Reads its agent name from env (set by the systemd unit, derived
     from the container hostname like `hermes-agent-pm` → `pm`)
  2. Loads its system prompt from the dashboard's postgres
  3. Polls for backlog tasks tagged for its role
  4. Atomically claims one with `FOR UPDATE SKIP LOCKED`
  5. Calls hermes-ollama via OpenAI-compat-ish /api/generate
  6. Writes the result back to the task + a task_message row
  7. Marks itself idle, sleeps, repeats

For v2: replace this entire script with `services.hermes-agent.enable`
from `nousresearch/hermes-agent/v2026.3.30` and an adapter shim that
bridges hermes-agent's task model to our postgres schema.
"""

from __future__ import annotations

import os
import sys
import time
import socket

import psycopg2
import requests

DATABASE_URL = os.environ["DATABASE_URL"]
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://hermes-ollama.local:11434")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "hermes3:3b")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "5"))
NUM_PREDICT = int(os.environ.get("NUM_PREDICT", "400"))
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.5"))

# Derive agent name from the container hostname.
# `hermes-agent-pm` → `pm`. If the env var AGENT_NAME is set, that
# overrides; otherwise we strip the conventional prefix.
def derive_agent_name() -> str:
    explicit = os.environ.get("AGENT_NAME")
    if explicit:
        return explicit
    host = socket.gethostname()
    if host.startswith("hermes-agent-"):
        return host[len("hermes-agent-"):]
    return host


AGENT_NAME = derive_agent_name()


def log(msg: str) -> None:
    print(f"[hermes-{AGENT_NAME}] {msg}", flush=True)


def db_connect():
    return psycopg2.connect(DATABASE_URL)


def wait_for(check_fn, name: str, max_attempts: int = 120, delay: float = 2.0) -> None:
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


def check_ollama() -> None:
    r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
    r.raise_for_status()


def get_agent_config() -> tuple[int, str]:
    """Return (agent_id, system_prompt) for our role.

    The dashboard seeds the agents table on startup with default roles
    (pm, coder, researcher, writer). If our name doesn't exist, we self-register
    with a generic prompt so the system keeps working.
    """
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, system_prompt FROM agents WHERE name = %s",
            (AGENT_NAME,),
        )
        row = cur.fetchone()
        if row:
            return row[0], row[1]

        # Self-register with a placeholder prompt
        log(f"agent '{AGENT_NAME}' not seeded by dashboard, self-registering")
        cur.execute(
            """
            INSERT INTO agents (name, role, system_prompt, status)
            VALUES (%s, %s, %s, 'idle')
            ON CONFLICT (name) DO NOTHING
            RETURNING id, system_prompt
            """,
            (
                AGENT_NAME,
                AGENT_NAME.capitalize(),
                f"You are {AGENT_NAME}, a hermes-fleet worker. Respond clearly and concisely to the task you are given.",
            ),
        )
        conn.commit()
        cur.execute(
            "SELECT id, system_prompt FROM agents WHERE name = %s",
            (AGENT_NAME,),
        )
        row = cur.fetchone()
        return row[0], row[1]


def heartbeat(agent_id: int, status: str, current_task_id=None) -> None:
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE agents
            SET status = %s,
                current_task_id = %s,
                last_seen_at = NOW()
            WHERE id = %s
            """,
            (status, current_task_id, agent_id),
        )
        conn.commit()


def claim_task(agent_id: int):
    """Atomically grab the oldest backlog task for our role.

    Uses `FOR UPDATE SKIP LOCKED` so multiple workers (in case of duplicate
    deploys) never claim the same task. Returns (task_id, title, description)
    or None.
    """
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE tasks
            SET status = 'in_progress',
                assigned_agent_id = %s,
                started_at = NOW()
            WHERE id = (
                SELECT id FROM tasks
                WHERE status = 'backlog'
                  AND assigned_role = %s
                ORDER BY created_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, title, description
            """,
            (agent_id, AGENT_NAME),
        )
        row = cur.fetchone()
        conn.commit()
        return row  # (id, title, description) or None


def call_ollama(system_prompt: str, title: str, description: str) -> str:
    user_msg = f"TASK: {title}"
    if description:
        user_msg += f"\n\nDETAILS:\n{description}"
    full_prompt = f"{system_prompt}\n\n{user_msg}\n\nRESPONSE:"

    r = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": CHAT_MODEL,
            "prompt": full_prompt,
            "stream": False,
            "options": {
                "temperature": TEMPERATURE,
                "num_predict": NUM_PREDICT,
            },
        },
        timeout=600,
    )
    r.raise_for_status()
    return r.json().get("response", "").strip()


def complete_task(task_id: int, output: str) -> None:
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE tasks
            SET status = 'done',
                output = %s,
                completed_at = NOW()
            WHERE id = %s
            """,
            (output, task_id),
        )
        cur.execute(
            """
            INSERT INTO task_messages (task_id, role, content)
            VALUES (%s, 'assistant', %s)
            """,
            (task_id, output),
        )
        conn.commit()


def fail_task(task_id: int, error: str) -> None:
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE tasks
            SET status = 'failed',
                error = %s,
                completed_at = NOW()
            WHERE id = %s
            """,
            (error, task_id),
        )
        conn.commit()


def main() -> int:
    log(f"agent worker starting (role: {AGENT_NAME})")
    log(f"  database: {DATABASE_URL.split('@')[-1] if '@' in DATABASE_URL else 'local'}")
    log(f"  ollama:   {OLLAMA_URL}")
    log(f"  model:    {CHAT_MODEL}")

    wait_for(check_db, "postgres")
    wait_for(check_ollama, "ollama")

    agent_id, system_prompt = get_agent_config()
    log(f"agent_id={agent_id}, system_prompt loaded ({len(system_prompt)} chars)")
    heartbeat(agent_id, "idle")

    while True:
        try:
            task = claim_task(agent_id)
            if task is None:
                heartbeat(agent_id, "idle")
                time.sleep(POLL_INTERVAL)
                continue

            task_id, title, description = task
            log(f"claimed task #{task_id}: {title}")
            heartbeat(agent_id, "busy", current_task_id=task_id)

            try:
                output = call_ollama(system_prompt, title, description)
                complete_task(task_id, output)
                log(f"completed task #{task_id} ({len(output)} chars)")
            except Exception as e:
                log(f"task #{task_id} failed: {e}")
                fail_task(task_id, str(e)[:1000])

            heartbeat(agent_id, "idle")
        except Exception as e:
            log(f"loop error: {e}")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
