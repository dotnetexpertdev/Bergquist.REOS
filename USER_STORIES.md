# Morning Assistant POC — complete build prompt for Claude Code

## Your assignment

Act as the implementing engineer. Work in the current project directory, expected to be `C:\Work\MorningAssistantPOC\POC`. Build and run the application described below. The deliverable is a working Python backend, React interface, database migrations, tests, and setup instructions. Read the existing files first. Preserve existing work, use the established project conventions where applicable, and implement each story through the actual interface. Start implementing after a short plan and continue through testing and handoff in this session.

Claude Code is the development tool. The running application uses the owner's **OpenAI API key** for document extraction, natural-language interpretation, and speech transcription. Use genuine Gmail, QuickBooks and OpenAI integrations. Implement the Todoist integration behind a configuration switch so the owner can enable it with credentials. Keep fake providers and sample responses confined to explicitly identified tests. If credentials are unavailable, finish the adapters, run deterministic tests, and identify the exact account setup and live checks still required. Never prompt the owner to paste secrets into chat.

## Who does what

- **Vendor** means the business requesting payment, for example the electricity company. The vendor sends its PDF bill to the owner's Gmail address.
- **Owner** means the person receiving and reviewing the bill. For this demo I am the owner, using my Gmail inbox and my QuickBooks test company. I use my browser on my computer or phone.
- **Application** means the software you are building. It imports the attachment when I click Import, preserves it, extracts the details, and shows them to me. When I approve a new entry, it creates an unpaid vendor bill and attaches the original. Money movement is outside the application.

## Demo goal

A vendor, such as the electricity company, emails a bill to you, the property owner. You receive the email in your Gmail inbox with the bill attached as a PDF. You label the email **Property Demo** and click **Import demo emails** on the private web page. The app saves the PDF and shows you the bill details. After you check the details and approve the entry, the app records an unpaid bill in your QuickBooks test company and attaches the PDF.

To test duplicate handling, you upload that same PDF from your phone. The app records both ways it received the PDF and keeps one QuickBooks bill. If the property is unclear, the app asks you to select it. Later, you can type or say “What happened to the electricity bill?” to find the original and see what happened.

**Scope assumption.** The two-day pilot uses one owner, one property, one Gmail inbox, one QuickBooks test company, PDF and image files, and typed or spoken questions and corrections. Todoist is a separately testable extension. If a real task is promised for the demo, include US-13 in the acceptance run. Multi-company operations, additional user roles, Slack, Folk, portfolio reporting, bank-feed matching, and payments belong to the broader product.

**Implementation split.** React provides one mobile-friendly page with import, upload, document list, detail, review, and search. Python owns sign-in checks, integrations, extraction, file preservation, duplicate checks, accounting writes, and history. For a single-instance demo, SQLite and a private persistent file directory are sufficient. Keep documents, arrivals, and external records separate: one document can have several arrivals, one QuickBooks bill ID, and optionally one Todoist task ID.

## Engineering blueprint

Use a small single-instance application. Default to FastAPI, SQLAlchemy with SQLite migrations, and React with TypeScript and Vite. Serve the built frontend and API from one origin. In development proxy API requests from Vite. Store timestamps in UTC, display them in the owner's configured timezone, keep invoice dates as calendar dates, and handle money as decimals with currency. Keep SQLite and original files outside the public frontend directory on persistent storage.

**Data model.** Give each document a stable ID and unique SHA-256 digest. Store its immutable blob location and content type. Store each submission separately with its method, source identity, original filename and timestamp. A Gmail message can have several attachments; store its raw email once. Store extracted fields separately from reviewed values and maintain a revision number. Include property mappings, company-scoped vendor and account references, approval snapshots, audit events, provider connections, operations, and remote record IDs. Use foreign keys, transactions and uniqueness constraints for identities. A new photo of the same bill can have its own source document while sharing the accounting link. Preserve each original filename as metadata.

**Workflow.** Received → reading → needs review or ready for approval → approved for posting → bill created, attachment pending → recorded with evidence. Also represent extraction failure, duplicate candidate, linked existing bill, and unknown remote outcome. Keep these distinct from the bill's payment status. An approval belongs to a specific reviewed revision and company. Changes invalidate any unused approval. Keep the file durable before extraction and the operation durable before any remote write. On restart surface unfinished work with a safe resume action. A small database-backed operation table is sufficient; avoid introducing a distributed queue for this pilot.

**Interface contract.** Expose authenticated operations for import, upload, list, detail, original download, edit, duplicate lookup/resolution, approval, attachment retry, search, and transcription. Use clear resource names and typed request/response models. Return the document ID, current revision, status, available actions and field-specific errors as appropriate. Reject stale revisions with a conflict; refetch before a new approval. Use GET for reads and authenticated mutation requests for actions. Handle empty, loading, disconnected, error, and completed states in React. Make buttons accessible by keyboard and supply text labels for audio controls. A reload of a document URL should open the same record.

**Configuration.** Include a secret-free environment template. List settings for the public origin, session secret, owner login, persistent data path, token encryption key, Gmail OAuth client/secret/callback and label, QuickBooks environment/client/secret/callback and expected company ID, OpenAI API key and model names, and optional Todoist project/token. Separate the examples from actual secrets. Use an authenticated owner session, secure cookies on HTTPS and CSRF protection for mutations. Validate OAuth state and callback configuration. Encrypt stored OAuth tokens with a server-held key and persist refreshed credentials. Keep real inbox contents, audio, tokens, and uploaded documents out of source control and logs. Show which integration needs setup without revealing credentials.

**Implementation rhythm.** First get a real document through upload → storage → detail. In parallelizable development stages add Gmail intake, extraction, review and the QuickBooks connection. Implement the accounting write only after duplicate checks and approval are in place. Make typed retrieval work before adding voice. Exercise both intake methods, every external-write failure path, and phone voice in the final rehearsal. Prioritize this sequence for the two-day demo and report incomplete work accurately.

## Day 1 — connections, intake, and reading

### US-01 · Open a private workspace
**As the owner, I want to open the same private workspace on my laptop and phone so I can review email attachments and upload a file from either device.**

**Implementation prompt.** Build the app entry point, sign-in, sign-out, session check, protected API routes and responsive shell. Put document list and review on the main screen with navigation to connection settings. Apply the same access check to binary download, search, transcription, and provider routes. Test an anonymous direct download and a signed-in phone-width viewport.

**Acceptance criteria**

- React fits a phone screen and supports a normal mobile file picker. A signed-in owner can open the list, upload, and inspect a document.
- Python requires owner authentication for documents, downloads, search, and actions, including direct requests using a known document ID. An unauthenticated request cannot retrieve a file.
- Store integration credentials on the server. Use persistent storage so a restart preserves files, decisions, and external record IDs.

### US-02 · Connect and verify one QuickBooks test company
**As the owner, I want to see which company is connected before I approve an accounting entry.**

**Implementation prompt.** Implement an owner-only Intuit OAuth flow, token refresh and connection status. Compare the authorized realm ID with the configured test company and fetch its display name. A developer sandbox and an ordinary QuickBooks test subscription use different credentials and API environments; document both and require deliberate selection. Load existing vendors, accounts and optional classes from this company. Test the wrong-company and reconnect cases with provider fixtures; reserve live writes for owner-approved test entries.

**Acceptance criteria**

- Configure the intended test environment, authorize it, and display the verified company name. Python checks the configured company ID before any accounting write.
- Select an existing test vendor, expense account, and class if the demo uses classes. First prove the connection by creating a separate disposable unpaid test bill and opening its attached PDF in QuickBooks.
- If authorization expires or the connected company differs from the configured company, show the issue and retain the local item for retry. Keep payment operations outside the integration.

### US-03 · Import only selected Gmail attachments
**As the owner, I want to select a bill a vendor sent to my Gmail inbox and import its attachment when I am ready to review it.**

**Implementation prompt.** Resolve the configured Gmail label to its ID and page through matching messages. Parse MIME attachments, including nested multiparts, decode actual bytes, preserve the raw labeled message, and call the common intake service. Use mailbox + message ID + stable MIME part identity to distinguish genuine arrivals from repeated syncs. Handle partial batches per attachment. Show imported, already processed, skipped and failed counts, with a retry control. If the label is missing, tell the owner how to create it in Gmail.

**Acceptance criteria**

- Connect Gmail read-only. Clicking **Import demo emails** lists messages with the configured **Property Demo** label. Fetch contents only for those messages.
- Process each supported attachment individually. Capture mailbox, message ID, attachment identity, filename, sender, receipt time, and source reference. Preserve the original email source for the labeled message and the original attachment bytes.
- A second import of the same message and attachment does not count as a new arrival. A genuinely new message containing the same PDF does count as a new arrival.
- Show import results, including skipped unsupported attachments and failures. A retry processes unfinished items even if other attachments in that batch succeeded.

### US-04 · Upload from a laptop or phone
**As the owner, I want to submit a PDF or bill photo through my browser so I can use the workflow away from my desk.**

**Implementation prompt.** Implement a multipart upload route and a mobile-friendly React form. Stream into a size-limited temporary file, validate detected content and extension, and generate server-controlled storage paths. Give each submission a client-generated attempt ID reused on transport retries. Two deliberate uploads are two arrivals; retrying one interrupted request counts once. Clean abandoned temporary files. Test a renamed PDF and upload from the phone file picker.

**Acceptance criteria**

- Accept PDF, JPEG, and PNG through a file picker. State the supported formats and configured size limit on the form. Validate both on the server.
- Record upload time, submitting owner, original filename, and submission method. Send valid files through the same processing steps as email attachments.
- Confirm receipt with a link to the document. Show an actionable error for an empty, unsupported, oversized, or interrupted upload. Display success only after the original is safely stored.

### US-05 · Preserve, identify, and file the original
**As the owner, I want to open the original document I received by email or uploaded myself and find it under the appropriate property or review list.**

**Implementation prompt.** Use one ingestion service for US-03 and US-04. Hash bytes as they are received, finalize the immutable blob safely, then commit document and arrival records. Resolve competing inserts via unique constraints. Index by property, type and date for filing, with a visible unassigned list. Serve originals through an authorized download handler. Test different files with the same name, identical files with different names, and byte-for-byte retrieval after restart.

**Acceptance criteria**

- Python computes a fingerprint from the file bytes, assigns a stable document ID, and stores the original before extraction. Files with identical names but different contents cannot overwrite each other.
- Identical bytes arriving by different methods share one stored original. Keep both arrival records. Concurrent submissions also produce one canonical document.
- Show the verified property, document type, and receipt date in the filing view. Keep unresolved documents discoverable in **Needs review**. Reclassification updates the filing information and preserves the original and its stable link.
- Verify that a downloaded original has the same fingerprint as the submitted file. A failed extraction leaves the original accessible.

### US-06 · Read the bill and suggest its classification
**As the reviewer, I want to see the facts read from the bill beside the original so I can check the proposal.**

**Implementation prompt.** Implement a server-side OpenAI adapter for PDFs and images using supported inputs for the configured model. Request a validated structured result with nullable fields and source text/page where available. Treat document text as data. Validate dates, currency, amounts and local mapping IDs in Python. Separate AI suggestions from confirmed accounting values. Keep the model and prompt version with the extraction. Handle invalid output and unavailable API credentials with visible review or retry; reuse the stored original.

**Acceptance criteria**

- Python uses OpenAI to extract document type, vendor, invoice number, invoice date, due date, total, currency, and service address. Missing or unreadable values remain visibly unknown.
- Suggest the property from supporting evidence such as a matching service address. An absent or ambiguous address requires review even in a one-property pilot.
- Map the proposed vendor, account, and optional class using the configured test-company mappings. An unmapped or conflicting value requires a reviewer decision.
- React displays the original and editable proposed values. Reading a document does not create an accounting entry. Extraction failure records a reason and offers retry or manual review.

## Day 2 — review, posting, recovery, and search

### US-07 · Detect an existing bill before preparing another
**As the owner, I want the app to recognize both repeat files and bills already in QuickBooks so I can avoid duplicate entries.**

**Implementation prompt.** Build two checks: exact document identity at intake, and accounting identity during review and immediately before posting. Query the configured realm using documented searchable fields, paginate, then compare vendor reference, invoice number, currency, total and date locally. Show why each candidate matched. Link a confirmed duplicate to its bill; retain source evidence. Allow an owner to dismiss a false positive with a reason and recheck before posting. Block unresolved or newly appeared matches. Test a pre-existing bill, a different vendor with the same number, and two different source files claiming the same bill.

**Acceptance criteria**

- First check the local repository for the file fingerprint and prior linked bill. Then check the connected company for a matching vendor and invoice number and compare amount, currency, and date.
- An exact PDF repeat links to the existing local record. A scan or photo may have a different fingerprint: preserve distinct source evidence and use invoice details to flag a possible match.
- For a bill entered directly in QuickBooks, show the candidate record and matching or conflicting fields. Allow the owner to confirm a link to the existing bill. A possible match or failed lookup blocks creation until resolved.
- Do not treat equal amounts alone as a confirmed duplicate. Repeat the accounting check immediately before creating a bill.

### US-08 · Resolve uncertain items and correct a classification
**As the reviewer, I want a visible list of items needing my decision and a clear question for each one.**

**Implementation prompt.** Build list filters and a document detail editor with the original and extracted fields visible together. Return the next required question from the backend. Validate account and vendor references against the connected company. Use versioned edits to prevent an old browser tab from overwriting a newer decision. Keep original values and record each correction. Test a missing property and a stale update from a second tab.

**Acceptance criteria**

- Show vendor, amount, status, and outstanding question in the list. Examples include “Which property is this for?” and “Is this the same invoice as bill 104?”
- Allow the reviewer to inspect the source, correct a value, and save. Record the old and new values, the actor, and the time. Re-run affected checks after the correction.
- Saving a correction and approving a QuickBooks entry are separate actions. An unresolved required field, failed duplicate check, or unresolved match keeps approval unavailable on both client and server.
- If an already-linked document is corrected locally, record the change and visibly flag any discrepancy with QuickBooks for follow-up.

### US-09 · Approve one unpaid bill and attach the evidence
**As the owner, I want to review the bill details and approve the app to record one unpaid bill in my QuickBooks test company.**

**Implementation prompt.** Implement a dedicated approve-and-create operation. Persist the exact approved values and revision. Serialize creation across documents claiming the same company/vendor/invoice identity, recheck the live company, and post a QuickBooks Bill using existing vendor and expense-account references. For a single-category demo bill, confirm the entire amount belongs to that category; route splits, tax treatment or unsupported currencies to review when the proposal cannot represent them correctly. Save the returned bill ID before uploading the file through the attachment API. Use a working company-specific navigation route or show the record reference and tested steps to locate it when direct links are unavailable. Never invoke a payment API.

**Acceptance criteria**

- The confirmation displays company, vendor, invoice number, total, account, property, and optional class. Only an explicit **Create unpaid bill** action starts the write.
- Python validates the current values and duplicate-check result, creates the bill, persists the returned bill ID, and attaches the original. Show completion only after attachment succeeds.
- Show the QuickBooks company, bill reference, accounting status, and a working route to open the record in the test company. Confirm the amount and PDF in QuickBooks during rehearsal.
- A second click, refresh, or simultaneous request produces at most one bill. If a reviewer links a pre-existing bill, record it as **Linked existing bill**, distinct from **Created bill**.

### US-10 · Recover from partial failures
**As the owner, I want to know exactly which step failed and retry it without repeating a completed action.**

**Implementation prompt.** Persist an operation key, target, payload fingerprint, attempt state and provider references. Reuse provider-supported request identifiers according to their current semantics. After uncertain bill creation, reconcile the original intent against QuickBooks before allowing a new attempt. After uncertain attachment upload, inspect attachments linked to that bill and verify available evidence; require review if identity remains uncertain. Test a timeout after the provider accepted the write, concurrent approvals, and a crash between bill creation and attachment. Fake the failures in tests; keep fault injection inaccessible in normal operation.

**Acceptance criteria**

- Persist the pending operation before a remote write. Reuse its operation identity on retry where supported. If a write times out with an unknown result, reconcile against QuickBooks before allowing another creation attempt.
- If the bill exists but the attachment failed, show **Bill created — attachment pending** with the bill ID and a **Retry attachment** action. Check for an existing attachment before uploading again.
- If the app cannot determine whether a remote write succeeded, leave the item in review with an explanation. A restart or repeated click must not silently mark it successful or blindly issue a new write.

### US-11 · See the source and action history
**As the owner, I want one record that shows how a bill arrived and what happened next.**

**Implementation prompt.** Create history entries at the time each step happens, in the same local transaction as the state change where possible. Use distinct events for email arrival, phone upload, review, approval, bill creation, linking, and attachment completion. The detail screen must explain who acted, when, and the result. Provide stable links to the original and local record, plus provider references. Test the exact two-arrival story after a restart.

**Acceptance criteria**

- Show originals, arrival methods and times, classification, corrections, duplicate decisions, approval, accounting result, attachment status, and failures in chronological order.
- Human actions identify the owner; automated actions identify the system. Each event survives a restart. Later corrections add history entries.
- The document page has a stable private link. An email arrival includes a source message reference, and an accounting entry includes its QuickBooks reference. A repeat upload shows the additional arrival on the same page.

### US-12 · Find a bill using everyday language
**As the owner, I want to ask “What happened to the power bill?” and get an answer I can verify.**

**Implementation prompt.** Implement a read-only search path using stored vendor, invoice number, service address, property, document type, dates and reviewed fields. Use OpenAI to interpret a natural-language question into validated search criteria; retrieve local candidates and ground each answer in the selected record and history. Keep canonical links and status under server control. Maintain the selected document for follow-up turns. Ask the owner to choose when candidates are ambiguous. Test imperfect descriptions, no result, multiple results, corrected data, and a follow-up about the selected bill.

**Acceptance criteria**

- React accepts a typed question or a reviewed voice transcript. Python searches stored facts and document descriptions, selects relevant records, and answers from their recorded status and history.
- “Power bill” can find the electricity bill even when the filename contains neither word. Return vendor, amount, property, latest action, and links to the original and history. Include the QuickBooks reference if available.
- With multiple plausible results, show choices. With no match, say so. An approved or recorded bill must not be described as paid based on those events.
- After the reviewer corrects a classification, a new question returns the corrected value and the history still shows the earlier value.

### US-13 · Create one linked Todoist review task (extension)
**As the owner, I want unresolved work in Todoist so a bill that needs my attention does not get forgotten.**

**Implementation prompt.** Ship a working Todoist client controlled by an enable flag. Create or update one review task in the configured project, persist the remote ID and the stable local document link, and handle ambiguous outcomes as in US-10. Use the current supported API and its documented request-id behavior. An unavailable Todoist service leaves a visible pending task action. Test with the switch both on and off; perform the live project check only when credentials are supplied.

**Acceptance criteria**

- On the first actionable review state, create a task in the configured demo project with company, vendor, reason, priority, review due date, and stable private document link. Use a configured review deadline if the bill has no due date, and label it as a review deadline.
- Store the task ID. When the item changes, update that task and add its QuickBooks reference once available. Re-importing or resubmitting a bill creates no extra task.
- If Todoist is unavailable, keep the document and accounting state intact, show task creation as pending, and reconcile before retrying an uncertain task write.

### US-14 · Speak to the assistant and hear its answer (core)
**As the owner, I want to ask about a bill or dictate a correction on my laptop or phone and hear the answer while I inspect its sources.**

**Implementation prompt.** Implement turn-based push-to-talk in React with browser microphone permission, supported MIME selection, stop and cancel, and an authenticated Python transcription endpoint. Validate audio type and length and release microphone tracks and transient audio when finished. Show the editable transcript, then use the same search or correction workflow as text. Dictated corrections are proposals and require explicit confirmation. Read the displayed answer aloud using device speech synthesis with Play/Stop. Cover microphone denial, empty audio, transcription failure and a follow-up turn. Provide an HTTPS phone rehearsal with a trusted certificate and a reachable host; a phone's localhost points to the phone.

**Acceptance criteria**

- React offers a microphone button with visible recording, stop, and cancel controls. Test on a private HTTPS page on the actual phone. Python sends the recorded turn to OpenAI for transcription. Show the editable transcript before submission.
- Send the transcript through the same retrieval or review workflow as typed input. A follow-up such as “Which property was it for?” uses the bill selected in the preceding turn. A dictated correction shows the proposed change and requires confirmation. Creating a bill still requires the existing explicit approval.
- Offer Play and Stop for the displayed answer using device speech synthesis for the pilot. Label playback as a synthetic voice and keep source links visible. If microphone permission is denied or transcription fails, keep the text box available and show retry. A cancelled recording triggers no action.
- Python loads the owner's OpenAI API key from a server environment variable or deployment secret. Document extraction, AI-assisted retrieval, and transcription use server-side calls. Keep the key out of the React bundle, browser storage, repository, and logs. If credentials fail, display a recoverable error and preserve the received document.
- Reference: [OpenAI speech-to-text guide](https://developers.openai.com/api/docs/guides/speech-to-text).

## Build order and acceptance run

**Day 1 checkpoint.** Verify OpenAI credentials with a short transcription (US-14), then verify QuickBooks and Gmail access (US-02 and US-03). Deliver the private page, common intake, storage, and extraction (US-01 and US-04 through US-06). By evening, a labeled email and a phone upload should open from the same document list.

**Day 2 checkpoint.** Implement duplicate decisions, owner review, unpaid bill creation, and recovery (US-07 through US-10). Verify history, retrieval, and the spoken interaction on laptop and phone (US-11, US-12, and US-14). Verify US-13 with isolated tests, and rehearse it live when Todoist is enabled.

**Fixtures.** Use three approved or redacted bills with known expected values. A is a clearly addressed electricity bill. B has an unclear property. C is a bill manually entered in the test company before the run. Record the starting QuickBooks bill count for each vendor and invoice number. Use a spare invoice for connection testing so A is genuinely new during the demo.

| Test | Action | Pass condition |
|---|---|---|
| 1. Email intake | In your Gmail inbox, label the email you received from the vendor with bill A attached. Leave an unrelated email unlabeled and click **Import demo emails** twice. | A appears once with one email arrival and its untouched original. The unrelated email is absent. |
| 2. Accounting | Review A and approve it. Repeat the click. | One unpaid bill in the intended company, correct values, one attached PDF, and a saved bill reference. |
| 3. Phone intake | Upload the same A from the phone under a different filename. | One stored PDF, two genuine arrivals, one QuickBooks bill, and one task if Todoist is enabled. |
| 4. Uncertainty | Upload B, inspect the question, and correct the classification. | B is visible before correction, owner decision is logged, and saving the change does not post a bill. |
| 5. Existing accounting record | Submit C for the first time in the app. | The app finds the manually entered QuickBooks bill, the owner can link it, and the remote bill count stays unchanged. |
| 6. Retrieval | Ask about the power bill and open the returned links. | The answer matches A’s actual history; the original and accounting record can be inspected. |
| 7. Failure rehearsal | Interrupt attachment after the test bill is created, restart, and retry. | The app resumes attachment against the same bill ID and records the recovery. |
| 8. Access and persistence | Open a private document link while signed out; then sign back in after restarting the app. | Signed-out access is denied; the owner still sees the original, corrections, and external IDs. |
| 9. Voice conversation | On the phone ask about the power bill, play the answer, ask which property it was for, then dictate a correction. Repeat a microphone check on the laptop. | The transcript is editable, the spoken answer agrees with the displayed record, the follow-up resolves the same bill, and correction waits for confirmation. Denying microphone access still allows typing. |

**Demo sign-off.** Record which tests passed against the actual test company and phone. If an external integration cannot be exercised, label that portion unverified and identify the last step that did run successfully.

## Required repository deliverables and verification

1. **Runnable source.** Provide the actual backend, frontend, initial migration, dependency manifests and reproducible installs. Include the owner bootstrap, configuration template, and a simple way to run the app from this Windows workspace. The default runtime must clearly show disconnected services until configured.
2. **Owner setup guide.** Write a README with exact PowerShell commands for install, migrate, initialize owner, run, test and build. Explain how to configure OAuth callback URLs, add the Gmail test user and label, authorize the QuickBooks test company and verify its realm, select vendor/account/class mappings, set the OpenAI key on the server and enable optional Todoist. Include phone HTTPS setup, data locations, and a backup/restore procedure that preserves both SQLite and original files.
3. **Tests that exercise the workflow.** Use pytest for ingestion, provider adapters and workflow state. Use React tests for review, approval, corrections and voice controls. Add a browser smoke test that signs in, uploads a fixture, reviews it, approves it, and verifies the linked record using isolated test providers. Use temporary storage and generated or redacted fixtures. Run migrations on an empty database, the backend tests, frontend checks/tests, browser smoke test and production build. Fix failures and record actual commands and results.
4. **Live rehearsal.** Use the acceptance table above with the owner's authorized accounts. Include one deliberate approval in the test company, one incoming labeled Gmail message, one real OpenAI extraction/transcription, and an actual phone upload. The test operator supplies test documents and confirms any destructive or accounting actions through the normal UI. Document manual checks that require the owner's device or credentials. Never report fixture-based evidence as live verification.
5. **Handoff.** Finish with the startup commands, URL, settings the owner must supply, stories and tests completed, and any outstanding live checks or limitations. A reviewer should be able to start from a clean checkout and reproduce the demo from the README alone.

**Provider documentation.** Check current official documentation when selecting models, API versions, scopes, and supported query fields. Use the provider examples as integration references and exercise the exact request shapes in tests. [Gmail message filtering](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list) · [Intuit OAuth](https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0) · [QuickBooks attachments](https://static.developer.intuit.com/output_html/qbo/docs/workflows/attach-images-and-notes.html) · [OpenAI speech input](https://developers.openai.com/api/docs/guides/speech-to-text) · [Todoist API](https://developer.todoist.com/api/v1/)
