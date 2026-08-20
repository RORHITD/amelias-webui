"""Project live agent sessions onto the Kanban board as derived cards.

The board's tasks live in ``hermes_cli.kanban_db`` -- an upstream package
(NousResearch/hermes-agent) whose schema has no concept of a session. Rather
than fork that schema, this module maps the sessions Hermes can already see
onto the same card shape, so a running or blocked agent appears on the board
beside hand-written tasks.

Derived cards are read-only. They carry ``derived: True`` so a client never
tries to PATCH one back into the task DB (there is no row to patch), and a
``session_id`` so a client can open the transcript instead.

The interesting column is ``blocked``: a session whose agent asked a question
nobody answered. That state is computed in ``api.models`` from the transcript
itself -- see ``_claude_code_agent_state``.
"""

from __future__ import annotations

import time

# A finished session stops being actionable quickly, and there are thousands of
# them on a working machine. Only surface recent ones, and cap the column, so
# 'done' never buries the two columns a person actually looks at.
DONE_MAX_AGE_SECONDS = 24 * 3600
DONE_MAX_CARDS = 12
# 'blocked' is the whole point of the board, so it gets a far looser cap --
# high enough to never hide a real question, low enough to bound the payload.
BLOCKED_MAX_CARDS = 50
RUNNING_MAX_CARDS = 25

_COLUMN_CAPS = {
    'running': RUNNING_MAX_CARDS,
    'blocked': BLOCKED_MAX_CARDS,
    'done': DONE_MAX_CARDS,
}


def _card(session: dict, now: float) -> dict:
    """Shape one session like a kanban task row so existing clients render it."""
    updated = session.get('last_message_at') or session.get('updated_at') or 0
    age = max(0, int(now - updated)) if updated else None
    question = session.get('blocked_question')
    title = session.get('title') or 'Untitled session'

    return {
        # Namespaced so a derived id can never collide with a real task id.
        'id': f"session:{session.get('session_id')}",
        'title': title,
        # The pending question is the single most useful thing on the card --
        # it is what the person has to answer to unblock the agent.
        'body': question or '',
        'status': session.get('agent_state') or 'running',
        'assignee': session.get('profile') or None,
        'priority': 0,
        'created_at': int(session.get('created_at') or 0),
        'started_at': None,
        'completed_at': None,
        'workspace_kind': 'session',
        'workspace_path': session.get('cwd'),
        'branch_name': session.get('git_branch'),
        'project_id': None,
        'tenant': None,
        'result': None,
        'age_seconds': age,
        'age': age,
        'progress': None,
        'link_counts': {'parents': 0, 'children': 0},
        'comment_count': 0,
        # --- derived-card fields (ignored by clients that don't know them) ---
        'derived': True,
        'read_only': True,
        'session_id': session.get('session_id'),
        'message_count': session.get('message_count'),
        'blocked_question': question,
        'source': session.get('source_tag'),
    }


def session_cards(sessions: list | None = None, now: float | None = None) -> list[dict]:
    """Derived cards for interactive sessions, newest first within each column.

    Automation sessions (hooks, scripts -- anything the SDK spawned rather than
    a person typing) are excluded. They are real sessions, but nobody is waiting
    on them, and on this machine they outnumber interactive ones roughly 4:1.
    """
    now = time.time() if now is None else now
    if sessions is None:
        from api.models import get_claude_code_sessions

        try:
            sessions = get_claude_code_sessions()
        except Exception:
            return []

    cards: list[dict] = []
    for session in sessions or []:
        if not isinstance(session, dict):
            continue
        if session.get('is_automation'):
            continue
        state = session.get('agent_state')
        if state not in _COLUMN_CAPS:
            continue
        updated = session.get('last_message_at') or session.get('updated_at') or 0
        if state == 'done' and (not updated or (now - updated) > DONE_MAX_AGE_SECONDS):
            continue
        cards.append(_card(session, now))

    cards.sort(key=lambda c: c.get('created_at') or 0, reverse=True)

    kept: list[dict] = []
    seen: dict[str, int] = {}
    for card in cards:
        status = card['status']
        count = seen.get(status, 0)
        if count >= _COLUMN_CAPS.get(status, 0):
            continue
        seen[status] = count + 1
        kept.append(card)
    return kept


def merge_into_columns(columns: list, sessions: list | None = None, now: float | None = None) -> list:
    """Append derived session cards to the matching board columns, in place.

    Unknown statuses are dropped rather than creating new columns: the board's
    column set is owned by the caller, not by whatever a transcript happens to
    say.
    """
    if not isinstance(columns, list):
        return columns
    by_name = {}
    for column in columns:
        if isinstance(column, dict) and isinstance(column.get('tasks'), list):
            by_name[column.get('name')] = column
    if not by_name:
        return columns

    for card in session_cards(sessions=sessions, now=now):
        column = by_name.get(card['status'])
        if column is not None:
            column['tasks'].append(card)
    return columns


def session_state_fingerprint(sessions: list | None = None) -> str:
    """A cheap value that changes whenever the derived cards would change.

    The board's ``since=`` short-circuit keys off ``latest_event_id`` from the
    task DB, which never moves when a *session* changes state. Without folding
    this in, an agent could go from running to blocked and the board would
    keep replying "nothing changed".
    """
    try:
        cards = session_cards(sessions=sessions)
    except Exception:
        return '0'
    if not cards:
        return '0'
    parts = [f"{c.get('id')}:{c.get('status')}:{c.get('age_seconds') is not None}" for c in cards]
    parts.sort()
    return str(hash(tuple(parts)) & 0xFFFFFFFF)
