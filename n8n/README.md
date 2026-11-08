# Pet Journal n8n pipeline

`workflows/morning.sdk.ts` is the portable, inactive SDK definition. `build.py` embeds the readable deterministic modules in Code nodes; it does not require network access. `config.example.json` contains only generic addresses/camera names. Actual deployment settings belong in the private n8n **Deployment settings** node or a gitignored `config.local.json`, never in a public workflow export.

## Ownership and flow

One morning workflow has a manual dry-run entry and an inactive daily 08:00 Schedule Trigger. Workflow timezone is America/Toronto. It uses calendar dates for yesterday and the preceding seven completed local dates; Sunday is the provisional weekly-selection day. A weekly run still saves daily reports and selects only the weekly report for the daily delivery slot.

The canvas separates calendar planning from the bounded request loop. **Dispatch action** routes requests to native HTTP Request nodes or the native Ollama image node. Code nodes perform deterministic planning, normalization, recursive window splitting, station grouping and structured-result validation; they perform no network I/O. The loop carries its state as paired n8n items and uses the server's run lease and checkpoints for safe retries.

The pipeline:

1. Check server readiness, claim a fenced run, and inspect the preceding week’s processing status without automatically backfilling it.
2. Read already-stored overlapping tracks for boundary context, then authenticate to Frigate with an encrypted credential. Collect only yesterday, plus a small configurable midnight context on either side. Recursively split saturated pages inside that bounded interval.
3. Normalize and deduplicate IDs, refresh known open source IDs, and group station detections before assigning ownership dates.
4. Persist raw events in bounded batches, mark processed dates, and transactionally replace candidates for the complete affected ownership interval.
5. Cache a full-context snapshot per visit. Verified bbox/clip capabilities can add evidence variants. Reuse classifications with matching evidence/grouping/model/prompt fingerprints; classify at most a configured number of uncached visits per run.
6. Validate model identity and evidence IDs, enums, rationale lengths and link restrictions. Invalid or failed inference leaves behavior unknown and preserves visits/evidence. Reports use server template summaries.
7. Prepare/finalize daily reports and optionally a weekly report. Claim today's delivery slot only when every independent permission is satisfied. The Home Assistant request has no automatic retry; uncertain outcomes become `unknown`.
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

`run_options` controls manual runs and defaults to `{ "dry_run": true, "notify": false, "mode": "auto" }`. `schedule_options` is separate and defaults to `{ "dry_run": false, "notify": true, "mode": "auto" }`, but no schedule runs until the workflow is explicitly published. In addition, delivery requires `notifications_enabled: true`, `delivery_verified: true`, the report server's own notification switch, and the run's explicit notify permission. Historical run dates can never send.

To process one historical day, set manual options to the following pattern (the run date is the following local date):

```json
{"dry_run":true,"notify":false,"mode":"daily","local_date":"2026-11-07","backfill_dates":["2026-11-06"]}
```

Restore normal manual options after the test. A dry run stores reports and evidence for review; it skips sending and destructive cleanup. It does not mean “no database writes.”

Normal runs **never automatically load history or catch up missing dates**. Each run processes one completed local day. On the weekly weekday, it processes yesterday as usual and builds the weekly report from the seven stored daily intervals. Missing days remain visibly partial; they are never downloaded just because a weekly report is due. Explicit manual backfill processes one requested day at a time.

The collection-only validation run disables inference and enforces 12 pages, 300 events and 500 KB of state. A subsequent inference validation run classifies at most three uncached visits. These limits fail closed rather than silently truncate. The generic example provides conservative defaults; increase them only after measuring the deployment.

## Completeness and grouping limits

The installed Frigate API was checked for `date_asc`, `after` and `before`; both time bounds are strict. Each recursively split query overlaps bounds slightly and deduplicates by source ID. Full pages are subdivided rather than advancing to their last timestamp. Dense timestamp ties, a non-progressing split, a page cap or an event cap fail closed as incomplete. This retrieves currently retained rows, not deleted footage or proof that cameras were recording.

The default Frigate interval is yesterday’s local midnight-to-midnight, expanded by 300 seconds on either side. The collector also reads already-stored overlapping rows and refreshes known ongoing IDs. If a reconciled visit owns a neighboring date, it loads that date’s stored context before replacing visits, protecting previous ownership without downloading historical days from Frigate. Missing known-open source rows make collection partial.

A finite boundary margin cannot prove there are no unknown tracks that began much earlier. Coverage remains unknown; review boundary cases and keep previously observed ongoing tracks refreshed. There is no claim of reconstructing deleted footage or all historical activity. Only compact grouping metadata and relevant payloads travel through the workflow; stored raw batches are released before media/inference work. The workflow enforces page, event and state-size limits.

Group membership is station-specific and deterministic by start time then ID. Finished overlapping fragments or gaps up to the provisional 60 seconds merge. Ongoing tracks remain separate/provisional and cannot bridge a chain. A source row listing two configured zones may support both stations; no zone chronology is inferred. Long/many-fragment groups are flagged for review in run output, never silently truncated. Expand affected calendar dates until every included finished boundary visit is owned and contained; reject ranges beyond 31 days or server batch limits.

The 60-second threshold is provisional: compare 30/60/120-second grouping against footage before activation. Model labels are suggestions; one snapshot may be insufficient. Classification is bounded, and unclassified visits retain unknown behavior. Inference fingerprints are deterministic cache identifiers, not security hashes; they include evidence SHA-256, grouping membership/version, model name and prompt version. A model digest is not exposed by the node catalog; change the prompt/config version or model tag when replacing model weights.

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

The Node tests cover fragmentation, station isolation, ongoing tracks, cross-midnight ownership, DST, saturated timestamp ties, invalid model evidence, dry-run guards and a simulated complete pipeline. A pinned n8n empty-day test verifies the native loop without contacting external services. The server's PostgreSQL suite covers authenticated visit-cache metadata and sanitized failure diagnostics.

The SDK must pass both node-config validation and workflow validation before saving. Build/save never means publish. Keep the workflow inactive until real snapshots, classification output, boundary cases, coverage wording, thresholds, Home Assistant service/group membership and phone behavior have been reviewed.

### Notification groups

Set `notification_service` in **Deployment settings** to exactly one of `notify.all_devices` (default), `notify.apple_devices`, or `notify.android_devices`. The workflow calls that group once; it never fans out to overlapping groups. The payload includes a report link for both iOS (`url`) and Android (`clickAction`). Changing the group does not reset the server’s household/date delivery fence. Native notification calls have retries disabled; ambiguous failures remain `unknown` and are not automatically resent. Manual tests keep notifications off.
