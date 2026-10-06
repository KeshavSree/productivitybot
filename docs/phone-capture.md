# Charles phone capture

The same Python handler accepts Discord messages and phone captures. The phone
sends text, not audio; Siri/Shortcuts handles dictation. No native iOS app is needed.

## 1. Enable the deployed API

Railway deploys this repository as one process running Discord, HTTP, and the
Notion sync worker together. Keep one replica and attach a persistent volume at
`/data` with `DATA_DIR=/data`; the task database, sync queue, and capture receipts
all live in `/data/charles.db`.

Creating the `/data` directory does not create a persistent Railway volume.
Before redeploying an existing worker without a volume, back up its database
using SQLite's backup API. A newly attached volume starts empty, so restore that
backup before resuming task capture. Keep the database backup out of Git.

Add these variables to the existing Railway service:

- `CAPTURE_TOKEN`: a private, random bearer credential of at least 32 characters.
  Generate one locally with:
  ```sh
  python -c 'import secrets; print(secrets.token_urlsafe(32))'
  ```
- `CAPTURE_USER_ID`: your numeric Discord user ID. Enable Discord Developer Mode,
  then copy your user ID. If `ALLOWED_USER_IDS` is set, this ID must be in that list.

Keep `DISCORD_TOKEN`, `NOTION_TOKEN`, and `NOTION_DATABASE_ID` as before. Railway
supplies `PORT`; the server listens on `0.0.0.0:$PORT` (8080 locally).

In the Railway service's Settings → Networking, generate a public domain if it
doesn't already have one. Point it at the HTTP server's port. Railway terminates
HTTPS; use its HTTPS address on the phone.

Open `https://YOUR-DOMAIN/health`. It should report:

```json
{"status":"ok","capture_enabled":true,"notion_configured":true}
```

Without both capture variables, Discord keeps working and `/capture` returns 503.
Setting only one variable or using an invalid credential/user ID fails startup
with a clear configuration error. `/health` checks the process, not whether Notion
is reachable. A Notion outage doesn't disable local capture.

## 2. Build one Shortcut named Charles

Use these actions in Apple's Shortcuts app. This is a setup recipe, not an
installed or device-tested shortcut. Action labels can vary by system language.

1. **Dictate Text**: select your language and stop after a pause (or on tap if you
   prefer). Store the result as `TaskText`. If it is empty, stop the shortcut.
2. Create a unique capture ID. A native-action recipe is **Current Date** →
   **Format Date** with custom format `yyyyMMddHHmmssSSS`, then **Random Number**
   between 100000 and 999999, and a **Text** action joining them with a hyphen.
   Store that string as `CaptureID`.
3. Create a **Dictionary** with two text fields:
   - `text`: the `TaskText` variable.
   - `request_id`: the `CaptureID` variable.
4. **Get Contents of URL**:
   - URL: `https://YOUR-DOMAIN/capture`
   - Method: `POST`
   - Headers: `Authorization` = `Bearer YOUR_CAPTURE_TOKEN` and
     `Content-Type` = `application/json`.
   - Request Body: JSON, with the dictionary's `text` and `request_id` values.
     Insert variables using Shortcuts' variable picker; don't send the literal
     words `TaskText` or `CaptureID`.
5. Read the returned dictionary. On success, **Get Dictionary Value** for
   `message`, then **Speak Text**. You can also show that message as a notification.
   If the response contains `error`, show/read that error instead of saying it
   was saved.

A successful capture means the changes are committed in SQLite. The usual first
response says "Saved; waiting for Notion sync." The sync worker is notified
immediately and retries failed updates every 30 seconds. `local_only` means the
Notion credentials haven't been configured; it doesn't mean a page was created.

### Preserve a capture across a connection failure

For retries across separate Shortcut runs, extend the same Shortcut with a
pending-request file:

- Before **Get Contents of URL**, serialize the dictionary as JSON and **Save File**
  to a fixed `Shortcuts/Charles/pending.json` location, with Ask Where to Save off.
  The file contains the text and request ID, not the bearer token.
- At the start of the Shortcut, check that location (disable erroring on a missing
  file). If a pending request exists, offer to retry it and use its saved dictionary
  instead of dictating new text or generating a new ID. Don't overwrite a pending
  capture with a new one.
- Delete the pending file only after a response includes the matching `request_id`.
  Responses such as `not_found` and `ambiguous` are processed results; read their
  message. A network error, `error` response, or cancellation leaves the file for
  the next retry.
- Retrying the same dictionary is safe after a timeout or redeploy. Reusing its ID
  with different text returns 409. Starting a fresh capture creates a new ID and is
  intentionally a new request, even if its text is identical.

First-time microphone, file, and network permissions must be approved on the
phone. Test both entrances while locked; any unlock requirement is controlled by
iOS and the actions you've chosen. The pending-file extension needs file access.

## 3. Use either entrance

- **Siri**: invoke Siri and say "Charles", wait for the speech-input prompt, then
  say the task. The exact one-sentence phrase "Siri, Charles, [arbitrary text]"
  isn't implemented by this Shortcut. If Dictate Text behaves differently through
  Siri on your device, test Ask for Input (Text) for the prompt; Lock Screen capture
  must still have a dictation step if you want it to start listening automatically.
- **Lock Screen**: customize the Lock Screen, replace a bottom control with the
  Shortcuts control, and choose Charles. Pressing it launches capture. It does not
  record only while your finger remains held down.

Examples:

- "do CS 373 homework" → classified and added to Notion.
- "CS373 homework is done" → completes your matching open task.
- "finished CS 373 homework" → completes it too.
- "complete CS 373 homework" → adds a task; it is not a completion command.

Completion matches your own open tasks, ignoring case and punctuation and allowing
predictable variations such as `CS373` / `CS 373`, `hw` / `homework`, and an initial
`do`. It does not guess a different assignment number. Ambiguous matches aren't
completed; use `/list` in Discord to choose one. Unclear categories go to Inbox;
use Discord's `/list` to move them.

## API contract

`POST /capture` requires `Authorization: Bearer <CAPTURE_TOKEN>` and JSON:

```json
{"request_id":"phone-20261006-unique","text":"do cs 373 hw"}
```

The credential maps to `CAPTURE_USER_ID` on the server; client-supplied user IDs are
rejected. Text is limited to 4000 characters and 50 entries. Request IDs must use
1–128 letters, digits, underscores, or hyphens. The endpoint accepts up to 30
requests per minute, including retries; 429 includes `Retry-After: 60`.

Successful processing returns HTTP 200:

```json
{
  "request_id":"phone-20261006-unique",
  "action":"added",
  "message":"Added Do cs 373 hw to CS 373. Saved; waiting for Notion sync.",
  "sync_status":"pending",
  "replayed":false,
  "tasks":[{"id":1,"content":"Do cs 373 hw","category":"CS 373","done":false}]
}
```

`action` is `added`, `completed`, `mixed`, `ambiguous`, `not_found`, or `empty`.
`sync_status` is `pending`, `synced`, `local_only`, or `not_applicable`.
Repeating a request returns its original outcome with `replayed: true` and its
current sync status. It doesn't add or complete a second task.

Errors return JSON with `error`: 400 invalid input, 401 incorrect credential,
409 conflicting ID, 413 oversized body, 415 wrong content type, 429 rate limit,
503 capture disabled, or 500 an internal error. For 500/network failures, preserve
and retry the original request ID and text.

The sync queue persists additions, category changes, completions, and deletions.
Charles adds a `Charles ID` rich-text property to the Notion database for recovery
when a successful page creation response is lost. Run one service replica against
this database; Notion doesn't provide an atomic unique constraint for that ID.

## Check on your iPhone

Test Siri and the Lock Screen control with a new task, its completion, a cancelled
capture, and a lost connection followed by retry. Verify only one task appears in
Notion after retry. Lock Screen/Face ID behavior and voice recognition need a real
phone check; automated backend tests don't validate those interactions.

Apple references: [Siri shortcuts](https://support.apple.com/en-ae/guide/shortcuts/apd07c25bb38/ios),
[JSON API requests in Shortcuts](https://support.apple.com/guide/shortcuts/request-your-first-api-apd58d46713f/10.0/ios/27).
Railway references: [Public networking](https://docs.railway.com/guides/public-networking),
[Healthchecks](https://docs.railway.com/guides/healthchecks).
