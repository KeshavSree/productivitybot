# Personal Siri correction

Siri still dictates text and the Tara Shortcut still posts `text` and
`request_id` to `/capture`. Python performs personal correction before task
matching and categorization. The spoken response remains the `message` field.

## Enable

Set Railway variables:

```text
CORRECTION_MODE=active
GROQ_API_KEY=<your Groq API credential>
CORRECTION_MODEL=openai/gpt-oss-120b
```

The Groq account's plan and model quotas apply. The model can be changed to
another Groq model supporting strict structured output. No paid plan is required
by Tara itself; check your account's available quota. Text and the retrieved
personal context are sent to Groq when the model is used.

Without a Groq key, confirmed dictionary, contextual phrase, and full-transcript
examples still apply. The model never receives audio.

`CORRECTION_MODE=shadow` records proposals but executes the original text.
`CORRECTION_MODE=off` bypasses correction. Active mode applies supported edits
and saves unresolved captures for review without changing tasks.

All speech memories and raw/corrected capture records live in `charles.db` on
the existing persistent Railway volume. Schema additions are automatic and
preserve existing tasks and capture receipts. Typed Discord text bypasses speech
correction, but Discord manages teaching and review.

## Learn while using it

- `/list` displays task IDs.
- `/correct task_id:123 intended:Do URBN video interview` changes that existing
  task's title, queues a title update for its existing Notion page, and stores a
  confirmed example. It does not change completion state or create another task.
- `/teach original:urban intended:URBN context:video interview` teaches a phrase
  replacement only when the literal contextual phrase is present. Leave context
  empty only for genuinely universal replacements.
- `/teach ... category:Stack` limits direct automatic replacement to an explicit
  Stack tag unless a contextual condition is also supplied. The LLM can consider
  it as evidence; a category guess alone is insufficient for direct replacement.
- `/captures` displays the five most recent raw/accepted captures, statuses,
  full capture IDs, and affected task IDs.
- `/review` lists pending captures. `/review capture_id:<id> intended:<what you meant>`
  resolves one and executes it once. Repeating the resolution returns its receipt.
- `/speech-memory` lists the latest fifteen active personal memories and IDs.
- `/forget-memory memory_id:123` disables a rule or example. It preserves the
  historical record; it does not delete tasks or change Notion.
- Moving a task's category in Discord supplies a confirmed category example.

One correction becomes a retrievable full-phrase example immediately. It is not
automatically promoted into a universal dictionary replacement. Model output
does not become confirmed training data. A lack of user feedback is not a
positive label.

## Retrieval and language patterns

Candidates are ranked using spelling and CMU English pronunciation similarity.
Unknown names fall back to spelling and taught aliases. The system compares
candidate categories rather than hard-filtering history through one early guess.

Prompts contain at most ten vocabulary entries, five examples, five contextual
rules, five tasks, and brief category information. Retrieved context is capped
at 7,000 UTF-8 bytes; raw input is separately capped at 4,000 characters. These
are byte/character limits, not an exact tokenizer guarantee.

Task retrieval covers up to 500 recent tasks, balanced with at most 60 per
category. Older terms remain available if explicitly taught. Memories remain
stored outside the prompt and currently use local linear ranking; a larger
collection may eventually need a more sophisticated search index.

Repeated two/three-word phrases from distinct confirmed examples provide compact
category language summaries. Existing automatic task labels are weaker context,
not confirmed identity evidence. No fine-tuning happens after each edit.

## Acceptance and failure behavior

Known context-qualified rules run first. The LLM proposes unique localized
replacements referencing retrieved evidence. Python independently checks span
boundaries, evidence, pronunciation/spelling support, conflicting vocabulary,
numeric order, negation, explicit category tags, and add/complete/cancel intent.

Siri does not expose the source audio or alternative transcripts to this flow.
Therefore these checks cannot prove that a plausible correction matches speech.
Major corrections require a confirmed example or sufficiently similar phrasing.
Numbers, dates, and changes of intent need explicit human resolution.

Conflicting or unsupported edits become pending captures. Negated completion
statements also require review. A model outage, timeout, or malformed response
falls back to the original transcript and existing handler; it is recorded as
`fallback`. This maintains capture availability but cannot recover an error
while the provider is unavailable. Cancellation utterances bypass the model.

Each model request has a five-second HTTP timeout; each whole capture has a
seven-second correction budget. Multi-task inputs use separate contexts and
cannot acquire new task separators through model edits. If that overall budget
expires, all pieces fall back to the original text.

Concurrent retries of the same request share a lock in the current single Python
worker. Tasks, correction outcome, and final receipt commit together. A receipt
is never reinterpreted using newer memories. An interrupted pre-commit attempt
can be retried using its durable raw capture. Multiple independent workers would
require a database-backed processing lease before enabling parallel deployment.

## Initial checks and field learning

The automated checks exercise stable contracts: unchanged text, contextual
aliases, novel names, conflicting rules, protected intent/numbers, isolation,
timeouts, shadow/off modes, structured provider requests, retries/restarts,
human resolution, category patterns, and Notion title updates/retries.

Provider responses in tests are mocked; those checks do not establish model
accuracy. No real Siri corpus or live Groq request has been evaluated yet.
Inspect `/captures`, correct wording, move categories, or disable overbroad
rules as you use Tara. Real failures should become regression examples.
Track damaging edits, missed corrections, unnecessary review, and latency
separately. Replaying a learned example measures recall, not generalization.

## Research informing the design

- [Noisy-clean phrase modeling](https://arxiv.org/abs/1802.02607): error evidence
  and language context contribute separately.
- [SpellMapper](https://www.isca-archive.org/interspeech_2023/antonova23_interspeech.pdf):
  retrieve a bounded personal vocabulary shortlist, then edit localized spans.
- [GEC-RAG](https://arxiv.org/abs/2501.10734): retrieve confirmed noisy/clean examples.
- [DOC-RAG](https://aclanthology.org/2024.lrec-main.457.pdf): use domain-specific
  language patterns. Tara adapts the idea; it does not implement that ASR model.
- [Selective classification](https://www.jmlr.org/papers/v11/el-yaniv10a.html):
  abstention trades automatic coverage for fewer damaging decisions.
