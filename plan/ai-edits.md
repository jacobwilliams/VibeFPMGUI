# Plan: AI edits to the current file

*Drafted 2026-10-13 · Status: phases 1–4 implemented (2026-10-13); phase 5 (checks after
Accept) not yet.*

> **As built**, compared with this plan:
> - Edit mode sends the file **without** line numbers (so SEARCH text is
>   copied exactly); Ask mode keeps them, for findings. Copied numbers are
>   still taken out when matching.
> - The outline of a big file comes from a scan of its text
>   (`ai_context.OUTLINE_RE`), not from fortls, so it works without the
>   language server.
> - With a selection, a small file is still sent whole; the request quotes
>   the selected lines and asks to change only them. Big files are sent in
>   part (the selection or the procedure at the cursor, plus the outline).
> - The worker's repetition guard ignored nothing at first: it cut edit
>   answers short (a REPLACE repeats SEARCH's unchanged lines). It now skips
>   lines in edit blocks and ``` code.
> - Measured token counts correct the estimate only after a first question
>   (on follow-ups Ollama reuses what it read and counts only the new part).
> - Cards of an earlier answer stay when a newer answer has no edits.

## Goal

Ask the local model (AI → Model) to change the file open in the editor:
"swap these loops", "add intent to every dummy argument", "turn this into a
pure function". The model proposes changes, each shown as a diff, and the
user accepts or rejects each one.

Follow-up requests continue the conversation ("now do the same for
`solve`"), without the conversation outgrowing the model's context window.

**Non-goals (for now):**
- edits across several files (refactoring a module and all its users);
- creating or deleting files;
- the model running commands (building, testing) by itself;
- writing to disk without the user saving: accepted edits change the editor's
  text only.

## What exists today

The *AI chat* in the Review panel (`review_monitor.py`, `main_window.send_chat`)
already has most of the plumbing:
- a conversation with history (`ReviewWorker(history=...)`), streamed answers,
  Stop, Copy, New Chat, Up/Down for earlier questions;
- the open file sent along with each question, numbered, when *Send the open
  file* is ticked (`ai.build_chat_prompt`);
- `LINE n: category: comment` lines parsed into the findings list and marked
  in the editor (`ai.parse_findings`, `EditorTab.show_review`);
- starting Ollama and downloading the model as needed (`_ensure_ollama`,
  `_check_model`), one AI request at a time.

What's missing: a format for edits, applying them, a way to review them, and
context management. Right now the whole file goes with **every** question,
and Ollama doesn't complain when a conversation is too long: it silently
drops the start, including the system prompt and its format rules
(`context_size` caps `num_ctx` at 32k).

## The edit format: search/replace blocks

The model writes each change as the exact old text and its replacement:

```
foo.f90
<<<<<<< SEARCH
    do i = 1, n
      do j = 1, m
=======
    do j = 1, m
      do i = 1, n
>>>>>>> REPLACE
```

Rules given to the model (in the edit system prompt):
- SEARCH holds lines copied **exactly** from the file, enough of them to be
  unique (usually 2–6 lines), without the line numbers shown in the prompt;
- one block per separate change; several blocks are fine;
- an empty SEARCH adds the REPLACE lines at the end of the file (rarely
  needed: better to anchor on a nearby line);
- an empty REPLACE deletes the SEARCH lines;
- explain the change briefly in prose, outside the blocks.

### Why not the alternatives

| Format | Problem with small local models |
|---|---|
| Unified diff | The `@@ -12,7 +12,8 @@` counts are often wrong, so the patch doesn't apply; context lines get paraphrased |
| Line numbers ("replace 30–34 with …") | Off by one often; every edit moves the later numbers |
| Whole file rewritten | Slow, uses the context twice, and models drop or "summarize" code they weren't asked to touch (`! ... rest unchanged`) |
| Search/replace | Found by content: no numbers to get wrong, still applies after other edits, and a wrong one fails visibly instead of landing in the wrong place |

Aider uses search/replace blocks with small models for these reasons.

### Applying an edit (`ai_edits.py`, pure functions)

```python
@dataclass
class Edit:
    search: str          # as the model wrote it
    replace: str
    start: int = -1      # where it applies in the current text (character offsets), -1: not found
    end: int = -1
    status: str = "pending"   # pending, applied, rejected, stale, failed
    problem: str = ""         # why it can't be applied ("not found", "matches 3 places")

def parse_edits(answer: str) -> list[Edit]          # also on a partial answer: only complete blocks
def locate(text: str, edit: Edit) -> Edit           # sets start/end or problem
def apply(text: str, edit: Edit) -> str             # the new text
```

`locate` tries, in order, stopping at the first match:
1. **Exact**: SEARCH appears once in the text.
2. **Line-ending and trailing-space tolerant**: compare line by line with
   `rstrip()`.
3. **Indentation tolerant**: compare with each line's leading spaces removed.
   On a match, REPLACE is re-indented by the difference between the file's
   indentation and the model's (models often shift a block by 2 or 4).
4. **Line numbers left in by mistake**: strip a leading `12| ` from each
   SEARCH line and try again (the prompt shows numbered lines).

The result is rejected, with a reason shown on its card, when SEARCH:
- matches nothing: "not found: the model's text doesn't match the file";
- matches more than once: "matches N places: ask for a more specific change".

The rule is to never guess: an edit applies exactly where it says, or not at
all.

Fortran specifics:
- Matching is case-sensitive: changing `INTEGER` to `integer` is an edit.
- `&` continuation lines and fixed form (columns 1–6, 72) need no special
  handling: they're just text. The prompt asks the model to keep the file's
  form.
- Tabs: compare after expanding to the editor's tab width.

## Asking for edits

### Ask or Edit

The chat box gets a two-way choice, **Ask | Edit** (a segmented button next
to the field):
- **Ask**: today's chat (`CHAT_SYSTEM_PROMPT`); answers can contain findings,
  but search/replace blocks in an Ask answer are only shown as text.
- **Edit**: `EDIT_SYSTEM_PROMPT`, which asks for search/replace blocks only
  for the requested change. The open file is always sent, so *Send the open
  file* is ticked and disabled.

Also:
- **AI → Edit with AI…** (⌥⌘E) focuses the chat box in Edit mode.
- **Edit Selection with AI…** (editor context menu, with a selection) does the
  same, limited to the selection (see "Context window").

### The edit system prompt (draft)

```
You change a programmer's Fortran file as they ask. Reply with:
1. One or two sentences on what you'll change.
2. One SEARCH/REPLACE block per change, in exactly this form:
<<<<<<< SEARCH
(lines copied exactly from the file, without the line numbers)
=======
(the new lines)
>>>>>>> REPLACE
Rules: change only what was asked; keep everything else exactly as it is
(names, comments, formatting, free or fixed form); SEARCH must be unique in
the file; never write "..." or "rest unchanged" inside a block. If the request
is unclear or unsafe, ask instead of editing.
```

`temperature` drops to 0.1 for edits (0.2 for reviews and chat).

## Reviewing the changes

The Review panel shows a list of **edit cards**, between the conversation and
the chat box (the findings list stays above), filled in as blocks finish
streaming:

```
┌ solver.f90 · line 30 ───────────────────────────── [Accept] [Reject] ┐
│ -    do i = 1, n                                                     │
│ -      do j = 1, m                                                   │
│ +    do j = 1, m                                                     │
│ +      do i = 1, n                                                   │
└──────────────────────────────────────────────────────────────────────┘
┌ solver.f90 · ⚠ not found: the model's text doesn't match the file ─ [Show] ┐
                                    [Accept All (3)]  [Reject All]
```

- The diff is made by `difflib.unified_diff` from the located old text and the
  replacement, so it's correct whatever the model wrote, colored with the
  palette's red and green, in the editor's font, with 2 lines of context.
- Clicking a card scrolls the editor to the place and highlights the old
  lines (a temporary extra selection, as Find does).
- **Accept**: applies the edit to the editor's text in one
  `beginEditBlock`/`endEditBlock`, so ⌘Z undoes it. The tab becomes unsaved
  (•), and fortitude, fortls and spelling re-check it as after typing. The
  card turns to "applied".
- **Reject**: the card turns to "rejected" (kept, greyed, for the record).
- **Accept All**: applies the pending ones in order from the end of the file
  up (so earlier offsets stay valid), as **one** undo step.
- **Before each apply, `locate` runs again on the current text.** The user may
  have typed meanwhile, or accepted another edit that touched the same lines.
  If the old text is gone, the card turns "stale: the code changed since"
  instead of applying.
- Edits that overlap each other: the second one turns stale once the first is
  applied, by the same check.
- Failed cards (not found / several places) offer **Show**, the block as the
  model wrote it, so the user can copy what's useful.

Optional, behind a setting: after Accept, run fortitude (it already runs on
change) and say in the card if new errors appeared on the changed lines.

## Context window

The budget, for each request:

```
system prompt  +  file (current version)  +  history  +  room for the answer
```

Rules:
1. **The file goes once per version.** `send_chat` keeps the text it last
   sent. A request carries the file only if it changed since; earlier copies
   in the history are replaced by `[foo.f90 as it was then: replaced by the
   newer version below]`. Accepting edits makes a new version, so the next
   request sends it again (and the model sees its own edits applied).
2. **History is trimmed from the oldest.** If the estimate exceeds ~75% of
   `num_ctx`, drop the oldest turns, never the system prompt, the current file
   or the newest turn. Turns are dropped as question+answer pairs. The answers
   kept are shortened: search/replace blocks become `[edit to lines 30–34:
   applied]` (or `rejected`). The model doesn't need the old text again, and
   it learns what was accepted.
3. **Room for the answer**: reserve `MAX_ANSWER_TOKENS` (currently used as
   `num_predict`).
4. **Measure, don't only estimate.** The final stream part carries
   `prompt_eval_count`: keep it, correct the chars/3 estimate with it, and
   show it in the panel ("context 14k of 32k"). When it's close to the limit,
   say so and suggest New Chat.
5. **The limit comes from the model.** Ask Ollama (`client.show(model)`,
   `model_info["<arch>.context_length"]`) instead of the fixed 32k cap. Cap
   it again by memory (a 7B model at 32k already needs ~6 GB).

### Big files and selections

Over a threshold (say 600 lines or ~40% of the budget), or with a selection,
send a **window** instead of the whole file:
- the selection (or the procedure around the cursor) with 20 lines on either
  side;
- an outline of the rest: module name, `use` lines, declarations of module
  variables, and the procedures' signatures, from fortls's document symbols
  (`LspClient.document_symbols`, already used for the editor's outline);
- the prompt says these are only parts of the file.

Search/replace still works on a window, because blocks are found in the
**whole** file. It also gives better results: small models edit a 60-line
routine far more reliably than a 2,000-line file.

## Architecture

| Piece | Where | What |
|---|---|---|
| `Edit`, `parse_edits`, `locate`, `apply` | new `vibefpmgui/ai_edits.py` | Pure text functions, no Qt: easy to test |
| `EDIT_SYSTEM_PROMPT`, `build_edit_prompt` | `ai_review.py` | The prompt; the window/outline for big files |
| Context budget | new `ai_context.py` (or in `ai_review.py`) | Estimate, trim history, placeholder old file versions, read the model's limit |
| Edit cards | `review_monitor.py` (or a small `edit_cards.py` widget) | Diff view, Accept/Reject/Show, Accept All |
| Wiring | `main_window.py` | Ask/Edit mode in `send_chat`, apply to the `EditorTab` with one undo step, re-locate before applying |
| Editor | `editor.py` | Highlight a card's lines; "Edit Selection with AI…" in the context menu |

The worker (`ReviewWorker`) doesn't change, apart from passing
`prompt_eval_count` in its stats.

## Phases

1. **Parsing and applying edits** (`ai_edits.py` + tests). No UI. This is
   the part everything else relies on, and it's fully testable without a
   model.
2. **Edit mode and edit cards.** The Ask/Edit choice, the edit prompt, cards
   with Accept/Reject/Accept All, re-locate before applying, undo. The whole
   file is sent, as the chat does today.
3. **Context management.** File once per version, history trimming,
   shortened old answers, measured token use shown in the panel, the model's
   own limit.
4. **Selections and big files.** Edit Selection with AI…, windows with an
   outline from fortls.
5. *(Optional)* Check after accepting: fortitude/compile errors on the
   changed lines, shown in the card.

Each phase is usable on its own: after phase 2 the feature works for files
of ordinary size; phases 3–4 make long conversations and big files reliable.

## Testing

No model in CI (as for the review: `FakeOllama` gives canned answers).
- **`ai_edits` unit tests**, the bulk:
  - exact match; trailing spaces; CRLF; indentation shifted by 2 and by 4
    (and re-indented REPLACE);
  - line numbers left in SEARCH;
  - not found; found twice; empty SEARCH (append); empty REPLACE (delete);
  - several blocks; blocks split across stream chunks (only complete ones
    parsed);
  - a block with `>>>>>>>` inside a code comment; text around blocks;
  - fixed-form file; tabs.
- **Panel/window tests with `FakeOllama`:**
  - an answer with two blocks gives two cards;
  - Accept changes the editor text and ⌘Z restores it;
  - Accept All is one undo step;
  - a card goes stale after the user edits the same lines;
  - a failed block shows its reason;
  - Edit mode always sends the file.
- **Context tests:**
  - the second request doesn't resend an unchanged file;
  - after an accepted edit it does, and the older copy becomes the
    placeholder;
  - history is trimmed oldest-first under a small fake limit;
  - shortened answers keep "applied"/"rejected".
- **Manual check with real models** (qwen2.5-coder 7B and 14B), on a few
  typical requests, to tune the prompt: add `intent`, swap loops, rename a
  local, make a function `pure`, add `implicit none`.

## Risks and open questions

- **Model quality is the main risk.** 7B models get simple, local edits
  right; multi-step refactors much less often. Failing visibly (not found,
  stale) limits the damage, but the experience depends on the model. Should
  the panel suggest a bigger model when edits keep failing?
- **Models that won't follow the format** (they rewrite the whole file, or use
  Markdown fences instead). Option: also accept a single ```fortran block
  that's the whole file, and turn it into one edit via `difflib` (shown as
  several hunks). Worth adding in phase 2 if testing shows it's common.
- **Edits outside the request** ("while I was there I also renamed…"). The
  cards show everything, so the user can reject them; the prompt discourages
  them.
- **Undo across several accepts**: each Accept is its own undo step. Is that
  what users expect, or should a whole answer be one step? (Accept All is one
  step.)
- **Saving**: accepted edits leave the tab unsaved. Should there be an
  "Accept All and Save"? Probably not at first.
- **Card placement**: in the Review panel (as planned), or inline in the
  editor (VS Code-style ghost diff)? Inline is nicer but far more work with
  `QPlainTextEdit`. Start with the panel.
- **Should Ask mode also turn blocks into cards?** Simpler to keep edits to
  Edit mode, so a plain question never produces edits.
