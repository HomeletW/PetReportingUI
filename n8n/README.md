# Pet Journal n8n pipeline

`workflows/morning.sdk.ts` defines one workflow containing the Manual run entry, generation and notification schedules, and bounded batch processor. `build.py` embeds the readable deterministic modules in Code nodes; it does not require network access. `config.example.json` contains only generic addresses/camera names. Actual deployment settings belong in the private n8n **Deployment settings** node or a gitignored `config.local.json`, never in a public workflow export.

## Ownership and flow

Click **Execute workflow** in **Pet Journal · Morning reports** to run on demand. **Run mode → dry_run** is the single user-facing mode setting: `false` processes and sends a real Home Assistant notification; `true` processes without notifications or destructive cleanup. The normal setting is `false`. Daily schedules use Toronto time: **00:30 generates reports without notifications**, and **08:00 sends the saved report without recollection or inference**. Both schedules require publication. Manual run still processes and notifies immediately (subject to the daily delivery fence).

The same workflow invokes its own **On-demand report batch** entry, classifying at most three uncached visits per child execution until every eligible visit for yesterday has a saved result, then completes generation. Manual runs make a final send-enabled pass; scheduled generation uses live, notification-disabled batches so retention cleanup still runs. Intermediate batches with deferred classifications finish at stage `classify`; only a live run finished without errors at stage `finished` qualifies for morning delivery. If the saved report is unavailable, stale, deleted, expired, or generation is unfinished, the morning path stops without sending or falling back to an older report. This keeps memory and model work bounded while retaining one workflow and one configuration/credential set.

America/Toronto calendar dates are used throughout. The run date is fixed before processing; crossing midnight stops orchestration rather than changing dates mid-run. Sunday selects the preceding seven completed local dates for weekly reporting. A weekly run also saves yesterday’s daily report and selects only the weekly report for the daily delivery slot. Daily report membership remains yesterday’s 00:00–24:00.

The two schedule-specific mode nodes stay outside the processing groups because they enter different stages; grouping them with those stages would create additional group entry points. The canvas separates calendar planning from the bounded request loop. **Dispatch action** routes requests to native HTTP Request nodes or the native Ollama image node. Code nodes perform deterministic planning, normalization, recursive window splitting, station grouping and structured-result validation; they perform no network I/O. The loop carries its state as paired n8n items and uses the server's run lease and checkpoints for safe retries.

The pipeline:

1. Check server readiness, claim a fenced run, and inspect the preceding week’s processing status without automatically backfilling it.
2. Read already-stored overlapping tracks for boundary context, then authenticate to Frigate with an encrypted credential. Collect only yesterday, plus a small configurable midnight context on either side. Recursively split saturated pages inside that bounded interval.
3. Normalize and deduplicate IDs, refresh known open source IDs, and group station detections before assigning ownership dates.
4. Persist raw events in bounded batches, mark processed dates, and transactionally replace candidates for the complete affected ownership interval.
5. Cache a full-context snapshot per visit. Verified bbox/clip capabilities can add evidence variants. Reuse classifications with matching evidence/grouping/model/prompt fingerprints; classify at most `max_classifications_per_run` uncached visits per child run (default three). Return the number deferred so the outer run loop can request another batch.
6. Validate model identity and evidence IDs, enums, rationale lengths and link restrictions. Invalid or failed inference leaves behavior unknown and preserves visits/evidence. Reports use server template summaries.
7. Prepare/finalize daily reports and optionally a weekly report. Claim today's delivery slot only when every independent permission is satisfied, no classification work is deferred, and the batch has no errors. The Home Assistant request has no automatic retry; uncertain outcomes become `unknown`.
8. Invoke cleanup (preview only during dry runs), then finish/release the run. Failures record only an allowlisted step, HTTP status and machine code, never upstream error text.

## Credential setup

Reuse these encrypted n8n credentials by name:

- **Pet Reporting API**: Simplified Custom Auth (`httpTemplatedCustomAuth`), template `{"headers":{"Authorization":"Bearer {{api_key}}"}}`, with the service token only in Placeholder Values.
- **Pet Reporting Frigate Login**: the same credential type, template `{"body":{"user":"{{username}}","password":"{{password}}"}}`, with username/password only in Placeholder Values.
- **Ollama account**: existing `ollamaApi` credential. The initial vision model is `qwen3-vl:8b-instruct`, discovered from the instance catalog.
- **Home Assistant account** (`homeAssistantApi`): select your existing Home Assistant connection on **Notify report devices**. The native node obtains its host and token from the encrypted credential store. Verify the `notify` service catalog before setting `delivery_verified: true`.

Assign the credentials to their respective HTTP nodes after SDK creation. Dynamic URLs cannot always be auto-matched by n8n. Do not put auth secrets in Set/Code nodes, pinned data or HTTP parameters. Frigate session cookies exist only in runtime request state, are cleared before the summary, and are never sent to the report API or placed in report links.

## Private deployment settings

Copy the example values into **Deployment settings** and replace origins, allowlisted camera/zone IDs, timezone and household/source IDs locally. They must match the report server's configuration. Do not paste the resulting node JSON into a public issue, commit, or workflow export.

**Run mode** contains `dry_run` (normally `false`), the matching timezone, and a bounded `max_batches` pass limit. The workflow automatically uses its own ID for batch calls; no installation workflow ID belongs in the SDK. Restrict **This workflow can be called by** to this same workflow in your private deployment settings. Source configuration and all credentials remain in **Deployment settings** and the native service nodes.

- `dry_run: false`: drain processing batches, then call Home Assistant once. This is also the manual notification test path.
- `dry_run: true`: drain processing batches without sending or destructive cleanup. Restore `false` afterward to make the normal manual/scheduled path send-enabled.
- Manual execution in the n8n editor does not require publication. Automatic scheduling remains inactive until explicitly published.

The batch entry accepts `intent` and `local_date`. `manual_batch` stores a dry, unsent processing batch; `manual_send` requests the final live pass. Delivery still requires `notifications_enabled: true`, `delivery_verified: true`, the report server’s own switch, and explicit notify permission. Historical dates cannot send. Daily delivery fencing applies to manual tests as well: accepted or ambiguous deliveries are never automatically resent. Historical backfill remains an explicit batch operation, outside the normal yesterday-only Manual run.

A dry run stores reports, evidence, and classifications for review; it does not mean “no database writes.”

Normal runs **never automatically load history or catch up missing dates**. Each run processes one completed local day. On the weekly weekday, it processes yesterday as usual and builds the weekly report from the seven stored daily intervals. Missing days remain visibly partial; they are never downloaded just because a weekly report is due. Explicit manual backfill processes one requested day at a time.

The collection-only validation run disables inference and enforces 12 pages, 300 events and 500 KB of state. Each inference batch classifies at most three uncached visits. The outer run loop keeps invoking child runs until the deferred count is zero. Child runs reuse cached images and classifications, while rereading the same bounded day to preserve reconciliation behavior. This favors bounded memory and restartability over minimizing source requests. These limits fail closed rather than silently truncate. The generic example provides conservative defaults; increase them only after measuring the deployment.

## Completeness and grouping limits

The installed Frigate API was checked for `date_asc`, `after` and `before`; both time bounds are strict. Each recursively split query overlaps bounds slightly and deduplicates by source ID. Full pages are subdivided rather than advancing to their last timestamp. Dense timestamp ties, a non-progressing split, a page cap or an event cap fail closed as incomplete. This retrieves currently retained rows, not deleted footage or proof that cameras were recording.

The default Frigate interval is yesterday’s local midnight-to-midnight, expanded by 300 seconds on either side. The collector also reads already-stored overlapping rows and refreshes known ongoing IDs. If a reconciled visit owns a neighboring date, it loads that date’s stored context before replacing visits, protecting previous ownership without downloading historical days from Frigate. Missing known-open source rows make collection partial.

A finite boundary margin cannot prove there are no unknown tracks that began much earlier. Coverage remains unknown; review boundary cases and keep previously observed ongoing tracks refreshed. There is no claim of reconstructing deleted footage or all historical activity. Only compact grouping metadata and relevant payloads travel through the workflow; stored raw batches are released before media/inference work. The workflow enforces page, event and state-size limits.

Group membership is station-specific and deterministic by start time then ID. Finished overlapping fragments or gaps up to the provisional 60 seconds merge. Ongoing tracks remain separate/provisional and cannot bridge a chain. A source row listing two configured zones may support both stations; no zone chronology is inferred. Long/many-fragment groups are flagged for review in run output, never silently truncated. Expand affected calendar dates until every included finished boundary visit is owned and contained; reject ranges beyond 31 days or server batch limits.

The 60-second threshold is provisional: compare 30/60/120-second grouping against footage before activation. Model labels are suggestions; one snapshot may be insufficient. Unclassified visits retain unknown behavior. Errors, missing evidence, the run loop’s bounded pass limit, or a no-progress condition stop processing instead of silently treating unfinished work as complete. Older reports are not automatically backfilled. Inference fingerprints are deterministic cache identifiers, not security hashes; they include evidence SHA-256, grouping membership/version, model name and prompt version. A model digest is not exposed by the node catalog; change the prompt/config version or model tag when replacing model weights.

## Notification copy and icon

Notifications use a friendly, dated daily/weekly title, the saved report’s camera visit counts, a short note when collection is partial, emoji, and a trailing report URL. They do not infer meals, health, or behavior from visit counts. The portrait already hosted at `/static/icon.png` is passed as `icon_url`, with `mdi:cat` as the status-bar icon. Display depends on the installed Home Assistant Companion app and OS.

For a notification-only manual run, start the existing **Morning schedule · 08:00** trigger manually; the saved-report path and once-per-day delivery guard still apply. To explicitly resend today’s saved notification, open **Notification options**, set the boolean **resend_notification** to `true`, and manually start **Morning schedule · 08:00**. The option only takes effect in a manual editor execution; automatic scheduled runs ignore it. Each manual execution uses its own resend key and delivery audit row, leaving the normal daily slot untouched. Retries within the same execution cannot send twice. Set the toggle back to `false` for normal manual behavior. Expired, deleted, historical or unfinished reports still cannot be sent.

## Privacy and execution retention

Set workflow settings before live testing or publication:

- `saveDataSuccessExecution: none`
- `saveDataErrorExecution: none`
- `saveManualExecutions: false`
- `saveExecutionProgress: false`
- `timezone: America/Toronto`

Completed executions may consequently be unavailable to the execution-details tool. Inspect the report server's run/processing/report state for durable checkpoints. Do not enable payload retention merely to debug cookies or image binaries. Errors have sanitized diagnostics on run records. There are no family failure alerts and no Error Trigger notification workflow.

## Tests and build

```sh
npm ci --prefix n8n
npm test --prefix n8n
python3 n8n/build.py
```

The Node tests cover fragmentation, station isolation, ongoing tracks, cross-midnight ownership, DST, saturated timestamp ties, invalid model evidence, dry-run guards and a simulated complete pipeline. Pinned n8n tests verify the batch processor and outer run loop without contacting external services. Orchestration tests cover full backlog draining, safe dry-run defaults, late work, no-progress limits, and midnight rollover. Run summaries expose attempted/saved/deferred classification counts and delivery state. The server's PostgreSQL suite covers authenticated visit-cache metadata and sanitized failure diagnostics.

The SDK must pass both node-config validation and workflow validation before saving. Build/save never means publish. Keep the workflow inactive until real snapshots, classification output, boundary cases, coverage wording, thresholds, Home Assistant service/group membership and phone behavior have been reviewed.

### Notification groups

Set `notification_service` in **Deployment settings** to exactly one of `notify.all_devices` (default), `notify.apple_devices`, or `notify.android_devices`. The workflow calls that group once; it never fans out to overlapping groups. The payload includes a report link for both iOS (`url`) and Android (`clickAction`). Changing the group does not reset the server’s household/date delivery fence. Native notification calls have retries disabled; ambiguous failures remain `unknown` and are not automatically resent. Manual tests keep notifications off.
