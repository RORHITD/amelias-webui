"""An assistant reply's Copy button must yield the whole reply, not one fragment.

One agent turn is rendered as several `.assistant-segment` nodes when the agent
interleaves text with tool calls. `copyMsg` used to read the nearest single
`[data-raw-text]` ancestor, so copying a reply that had run a tool handed back
only the segment you happened to click next to — the user-visible complaint
being "I can't copy everything it recommended."

`_assistantTurnCopyText` gathers every text segment of the enclosing
`.assistant-turn`. Cards that own their own copyable payload (tool results,
thinking blocks, compression references) must keep single-node scope, or their
copy button would hand back the answer instead of the card.

Driven through Node against a minimal DOM stub, so these assert behaviour
rather than the presence of a source string.
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required to drive the handler")


def _ui_js() -> str:
    return UI_JS_PATH.read_text(encoding="utf-8")


def _harness() -> str:
    src = _ui_js()

    const_match = re.search(r"const _SELF_SCOPED_COPY_CARDS=.*?;", src)
    assert const_match, "_SELF_SCOPED_COPY_CARDS constant must exist"

    return f"""
{const_match.group(0)}
{extract_function(src, "_assistantTurnCopyText")}

// ── Minimal DOM stub ──────────────────────────────────────────────────────
// Supports only the selector forms the function under test uses: a
// comma-separated list of `.class` and `[attr]` terms.
function _matchesTerm(node, term) {{
  term = term.trim();
  // Compound (`.assistant-segment[data-raw-text]`) must be tested before the
  // bare `.class` form, or the whole term is read as one class name.
  const compound = term.match(/^\\.([\\w-]+)\\[([\\w-]+)\\]$/);
  if (compound) return node.classes.includes(compound[1]) && compound[2] in node.attrs;
  if (term.startsWith('.')) return node.classes.includes(term.slice(1));
  if (term.startsWith('[') && term.endsWith(']')) return term.slice(1, -1) in node.attrs;
  throw new Error('unsupported selector term: ' + term);
}}
function _matches(node, selector) {{
  return selector.split(',').some(term => _matchesTerm(node, term));
}}

class Node {{
  constructor(classes = [], attrs = {{}}) {{
    this.classes = classes;
    this.attrs = attrs;
    this.children = [];
    this.parent = null;
    this.dataset = {{}};
    if ('data-raw-text' in attrs) this.dataset.rawText = attrs['data-raw-text'];
  }}
  add(child) {{ child.parent = this; this.children.push(child); return child; }}
  closest(selector) {{
    let n = this;
    while (n) {{ if (_matches(n, selector)) return n; n = n.parent; }}
    return null;
  }}
  _descendants(out = []) {{
    for (const c of this.children) {{ out.push(c); c._descendants(out); }}
    return out;
  }}
  querySelectorAll(selector) {{
    return this._descendants().filter(n => _matches(n, selector));
  }}
}}

function segment(text) {{
  return new Node(['assistant-segment'], {{ 'data-raw-text': text }});
}}

const scenarios = {{}};

// A reply split by a tool call: two text segments, one tool card between them.
{{
  const turn = new Node(['assistant-turn']);
  const first = turn.add(segment('Checking Search Console.'));
  turn.add(new Node(['tool-card'], {{ 'data-raw-text': 'grep -r seo' }}));
  const last = turn.add(segment('## The 30-day goal\\n\\n- 427K impressions'));
  const btn = last.add(new Node(['msg-copy-btn']));
  const firstBtn = first.add(new Node(['msg-copy-btn']));
  scenarios.splitReplyFromLast = _assistantTurnCopyText(btn);
  scenarios.splitReplyFromFirst = _assistantTurnCopyText(firstBtn);
}}

// A tool card's own copy button stays scoped to the card.
{{
  const turn = new Node(['assistant-turn']);
  turn.add(segment('Answer text'));
  const card = turn.add(new Node(['tool-card'], {{ 'data-raw-text': 'tool payload' }}));
  const btn = card.add(new Node(['msg-copy-btn']));
  scenarios.toolCardButton = _assistantTurnCopyText(btn);
}}

// Thinking and compression cards likewise.
{{
  const turn = new Node(['assistant-turn']);
  turn.add(segment('Answer text'));
  const thinking = turn.add(new Node(['thinking-card']));
  scenarios.thinkingButton = _assistantTurnCopyText(thinking.add(new Node(['msg-copy-btn'])));
  const compression = new Node(['compression-turn']);
  scenarios.compressionButton = _assistantTurnCopyText(compression.add(new Node(['msg-copy-btn'])));
}}

// A button outside any assistant turn (e.g. a user row) falls through.
{{
  const row = new Node(['msg-row']);
  scenarios.outsideTurn = _assistantTurnCopyText(row.add(new Node(['msg-copy-btn'])));
}}

// Blank segments are dropped; duplicates are not repeated.
{{
  const turn = new Node(['assistant-turn']);
  turn.add(segment('  '));
  turn.add(segment('Only real text'));
  turn.add(segment('Only real text'));
  const btn = turn.add(segment('Tail')).add(new Node(['msg-copy-btn']));
  scenarios.blanksAndDuplicates = _assistantTurnCopyText(btn);
}}

console.log(JSON.stringify(scenarios));
"""


def _run() -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tf:
        tf.write(_harness())
        path = tf.name
    try:
        proc = subprocess.run([NODE, path], capture_output=True, text=True, timeout=30)
    finally:
        Path(path).unlink(missing_ok=True)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def scenarios() -> dict:
    return _run()


def test_copy_joins_every_segment_of_a_reply_split_by_a_tool_call(scenarios):
    expected = "Checking Search Console.\n\n## The 30-day goal\n\n- 427K impressions"
    assert scenarios["splitReplyFromLast"] == expected


def test_copy_is_the_same_from_any_segment_of_the_reply(scenarios):
    assert scenarios["splitReplyFromFirst"] == scenarios["splitReplyFromLast"]


def test_tool_card_payload_is_excluded_from_the_reply_text(scenarios):
    assert "grep -r seo" not in scenarios["splitReplyFromLast"]


def test_self_scoped_cards_keep_their_own_copy_scope(scenarios):
    # Empty means "fall through to the single-node path" in copyMsg.
    assert scenarios["toolCardButton"] == ""
    assert scenarios["thinkingButton"] == ""
    assert scenarios["compressionButton"] == ""


def test_button_outside_an_assistant_turn_falls_through(scenarios):
    assert scenarios["outsideTurn"] == ""


def test_blank_segments_dropped_and_duplicates_not_repeated(scenarios):
    assert scenarios["blanksAndDuplicates"] == "Only real text\n\nTail"


def test_copy_msg_prefers_turn_text_then_falls_back():
    """copyMsg must try the turn first and keep the single-node path as fallback."""
    src = _ui_js()
    fn = extract_function(src, "copyMsg")
    assert "_assistantTurnCopyText(btn)" in fn
    assert "closest('[data-raw-text]')" in fn
    assert fn.index("_assistantTurnCopyText(btn)") < fn.index("closest('[data-raw-text]')")
