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

import json
import os
import re
import sys
import time
import socket

import psycopg2
import requests

# ─── Autonomous delegation chain ────────────────────────────────────────────
# v1.9 hierarchy: atlas → orion → {forge, vesper, lyra, echo, sage}
# When atlas finishes a task, it asks the LLM to break it into 1-3 orion
# subtasks. Orion does the same for specialists. Whole chain hands-off.
DELEGATION_TARGETS = {
    "atlas": ["orion"],
    "orion": ["forge", "vesper", "lyra", "echo", "sage"],
}

DATABASE_URL = os.environ["DATABASE_URL"]
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://hermes-ollama.local:11434")
FALLBACK_MODEL = os.environ.get("CHAT_MODEL", "hermes3:3b")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "5"))
NUM_PREDICT = int(os.environ.get("NUM_PREDICT", "400"))
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.5"))

# Strip thinking-model <think>...</think> blocks from model output so tasks
# get clean answers regardless of which model is active.
THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)

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
    """Atomically grab the oldest `todo` task for our role.

    `backlog` is the manual staging column — humans drop tasks there, then
    move them to `todo` when they're ready for an agent to pick up.
    `todo` is the queue agents poll. This mirrors the OwnStartup design.

    Uses `FOR UPDATE SKIP LOCKED` so multiple workers (in case of duplicate
    deploys) never claim the same task.
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
                WHERE status = 'todo'
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


def get_active_model() -> str:
    """Read the currently selected model from the settings table.

    The dashboard writes to `settings.active_model` when the user picks a
    new model in the UI. Workers read on every task claim so swaps take
    effect immediately. Falls back to the env var if the table or row is
    missing (first boot, etc).
    """
    try:
        with db_connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM settings WHERE key = 'active_model'"
            )
            row = cur.fetchone()
            if row and row[0]:
                return row[0]
    except Exception:
        pass
    return FALLBACK_MODEL


def call_ollama(system_prompt: str, title: str, description: str) -> str:
    user_msg = f"TASK: {title}"
    if description:
        user_msg += f"\n\nDETAILS:\n{description}"
    full_prompt = f"{system_prompt}\n\n{user_msg}\n\nRESPONSE:"
    model = get_active_model()

    r = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": model,
            "prompt": full_prompt,
            "stream": False,
            # Disable thinking on models that support the flag (qwen3, deepseek-r1).
            "think": False,
            "options": {
                "temperature": TEMPERATURE,
                "num_predict": NUM_PREDICT,
            },
        },
        timeout=600,
    )
    r.raise_for_status()
    resp = r.json().get("response", "").strip()
    # Belt-and-braces strip for models that ignore the think flag
    resp = THINK_RE.sub("", resp).strip()
    return resp


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


def get_project_id(task_id: int) -> int:
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT project_id FROM tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
        return row[0] if row else None


def insert_subtask(parent_id: int, project_id: int, role: str, title: str, description: str) -> int:
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tasks (project_id, parent_task_id, title, description,
                               assigned_role, status, assignees)
            VALUES (%s, %s, %s, %s, %s, 'todo', %s)
            RETURNING id
            """,
            (project_id, parent_id, title[:300], description, role, [role]),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        return new_id


def parse_subtask_json(text: str) -> list:
    """Pull a JSON array of subtasks out of an LLM response.

    The model isn't perfectly reliable about JSON formatting so we try
    a few strategies: direct json.loads, then a regex hunt for the
    first `[...]` block. If everything fails we return an empty list
    and the worker just doesn't delegate this round (no harm done).
    """
    text = text.strip()
    # Strip code fences
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def delegate_subtasks(parent_id: int, parent_title: str, parent_description: str,
                      parent_output: str) -> int:
    """Ask the LLM to break the just-finished task into subtasks for the next tier.

    Returns the number of subtasks created. Safe to no-op if our role
    isn't a delegating role or the LLM output can't be parsed.
    """
    targets = DELEGATION_TARGETS.get(AGENT_NAME)
    if not targets:
        return 0

    project_id = get_project_id(parent_id)
    if not project_id:
        return 0

    target_label = " or ".join(targets)
    delegation_prompt = (
        f"You just finished a task as the {AGENT_NAME}. Now break it into "
        f"1-3 concrete subtasks for your team ({target_label}).\n\n"
        f"ORIGINAL TASK: {parent_title}\n"
        f"DETAILS: {parent_description or '(none)'}\n\n"
        f"YOUR COMPLETED OUTPUT:\n{parent_output[:1500]}\n\n"
        f"Reply with ONLY a JSON array. Each subtask MUST have: "
        f'"role" (one of: {", ".join(targets)}), "title" (max 80 chars), '
        f'"description" (1-2 sentences). Format example:\n'
        f'[{{"role": "{targets[0]}", "title": "...", "description": "..."}}]\n\n'
        f"JSON:"
    )

    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": get_active_model(),
                "prompt": delegation_prompt,
                "stream": False,
                "think": False,
                "options": {"temperature": 0.3, "num_predict": 500},
            },
            timeout=600,
        )
        r.raise_for_status()
        raw = r.json().get("response", "").strip()
        raw = THINK_RE.sub("", raw).strip()
    except Exception as e:
        log(f"delegation LLM call failed: {e}")
        return 0

    items = parse_subtask_json(raw)
    if not items:
        log(f"delegation: could not parse JSON from LLM (got {len(raw)} chars)")
        return 0

    created = 0
    for item in items[:3]:  # cap at 3 to avoid LLM going wild
        try:
            role = str(item.get("role", "")).strip().lower()
            title = str(item.get("title", "")).strip()
            desc = str(item.get("description", "")).strip()
            if not title or role not in targets:
                continue
            new_id = insert_subtask(parent_id, project_id, role, title, desc)
            log(f"  delegated → #{new_id} [{role}] {title[:60]}")
            created += 1
        except Exception as e:
            log(f"delegation insert failed: {e}")
    if created:
        log(f"delegation: spawned {created} subtask(s) for {target_label}")
    return created


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
    log(f"  model:    (dynamic — reading from settings.active_model, fallback={FALLBACK_MODEL})")

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
                # Autonomous delegation: if our role spawns work for a
                # next tier (ceo→pm, pm→specialists), ask the LLM to
                # break this into subtasks. Failures are non-fatal.
                if AGENT_NAME in DELEGATION_TARGETS:
                    try:
                        delegate_subtasks(task_id, title, description, output)
                    except Exception as e:
                        log(f"delegation chain error (ignored): {e}")
            except Exception as e:
                log(f"task #{task_id} failed: {e}")
                fail_task(task_id, str(e)[:1000])

            heartbeat(agent_id, "idle")
        except Exception as e:
            log(f"loop error: {e}")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
