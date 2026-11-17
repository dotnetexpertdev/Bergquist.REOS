# Morning Assistant POC

A vendor emails a PDF bill to your Gmail. You label it **Property Demo**, click **Import From Inbox**, review what was read, and explicitly approve **one unpaid bill** in your QuickBooks test company, with the original PDF attached. If you later upload the same PDF from your phone, the app records a second arrival, but you still have one stored original and one QuickBooks bill. You can type or say “What happened to the electricity bill?” and get an answer built from the saved history, with links to the sources.

* Backend: Python 3.11+ (FastAPI, SQLAlchemy, SQLite, Alembic), in `backend\`
* Frontend: React + TypeScript + Vite, in `frontend\`. In normal use the backend serves the built UI and the API from one address.
* The app never moves money. It only creates **unpaid** bills, and only after you click **Create unpaid bill**.

All commands below are for **Windows PowerShell**, run from the project folder (`C:\work\POC`) unless a step says otherwise.

---

## 0. Notes for this workspace (read first)

* Your settings file is currently `backend\.env`. That location still works: the app uses the root `.env` if one exists, otherwise `backend\.env`. `python backend\cli.py doctor` shows which file and which database are in use.
* `DATA_PATH=./data` is resolved relative to the folder that holds the `.env` file. Your database therefore stays at `backend\data\morning_assistant.db`, wherever you start the app from.
* Your `.env` uses the older callback paths (`/api/auth/gmail/callback` and `/api/auth/quickbooks/callback`). The app now serves those paths as well, so whatever you already registered with Google and Intuit keeps working. Use `doctor` to see the exact URLs.
* `OWNER_PASSWORD_HASH` is empty in your `.env`, so you can't sign in yet. Run step 3b.
* Your existing database was created before migrations were used. **Back it up (step 4), then run `migrate` once.** Migrate keeps your existing rows (your “Demo Property”) and only adds new columns and tables.

---

## 1. Prerequisites

* Python 3.11 or newer (3.12 is tested), Node.js 18 or newer (24 is tested)
* For phone voice testing: an HTTPS address your phone can reach (see section 9)

## 2. Install

```powershell
cd C:\work\POC
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r backend\requirements.txt

cd frontend
npm ci
npx playwright install chromium     # only needed for the browser test
cd ..
```

Pinned versions that matter: `bcrypt==5.0.0` is used directly for password hashing (Passlib was removed because it crashes with current bcrypt), and `openai==1.109.1` supports PDF file inputs.

## 3. Configure

```powershell
# a) Settings file (skip this if you keep using backend\.env)
copy .env.example .env
.\venv\Scripts\python.exe backend\cli.py generate-keys    # paste SESSION_SECRET and TOKEN_ENCRYPTION_KEY into .env

# b) Owner login: prints OWNER_USERNAME / OWNER_PASSWORD_HASH lines to paste into the .env file
.\venv\Scripts\python.exe backend\cli.py create-owner

# c) Check what the app will use (prints no secrets)
.\venv\Scripts\python.exe backend\cli.py doctor
```

Put every secret in the `.env` file yourself, in a text editor. Never paste keys into chat, the browser, or a ticket. The OpenAI key is read only by the Python server.

## 4. Back up, then create or upgrade the database

```powershell
# Back up the database AND the original files (run while the server is stopped)
$data = (.\venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'backend'); from app.config import settings; print(settings.data_path)")
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
Compress-Archive -Path "$data\*" -DestinationPath "backup-$stamp.zip"

# Create or upgrade the schema (safe to run again)
.\venv\Scripts\python.exe backend\cli.py migrate

# Optional: add a property. Its address is used to match a bill's service address
.\venv\Scripts\python.exe backend\cli.py create-property "Demo Property" --address "123 Main St, Springfield"
```

The server never creates a database by itself. If the database is missing or not migrated, it stops and tells you to run `migrate`, so starting from another folder can't quietly create a second, empty database.

## 5. Start the app

```powershell
cd frontend; npm run build; cd ..                          # build the UI once (again after UI changes)
.\venv\Scripts\python.exe backend\cli.py serve              # http://localhost:8000
```

Open <http://localhost:8000> and sign in as the owner. To allow your phone on the same Wi-Fi, use `serve --host 0.0.0.0`. Voice input on a phone also needs HTTPS (section 9).

Optional development mode with hot reload, in two terminals:

```powershell
.\venv\Scripts\python.exe backend\cli.py serve --reload
cd frontend; npm run dev        # http://localhost:5173 (API calls are proxied to :8000)
```

## 6. Register the OAuth callback URLs exactly

Print the exact values for your configuration with `.\venv\Scripts\python.exe backend\cli.py doctor`. Each redirect URI is `PUBLIC_ORIGIN` followed by the callback path:

| Provider | `.env` setting | Default value | Register this exact URI |
|---|---|---|---|
| Google (Gmail) | `GMAIL_OAUTH_CALLBACK` | `/api/gmail/callback` | `http://localhost:8000/api/gmail/callback` |
| Intuit (QuickBooks) | `QUICKBOOKS_OAUTH_CALLBACK` | `/api/quickbooks/callback` | `http://localhost:8000/api/quickbooks/callback` |

With **your current `backend\.env`**, the URIs are `http://localhost:8000/api/auth/gmail/callback` and `http://localhost:8000/api/auth/quickbooks/callback`. The app now handles both forms. The value registered with the provider, the value in `.env`, and the address you browse to must all match exactly, including `http` versus `https`, the host and the port. If you use a tunnel for the phone (section 9), register the tunnel URIs as well.

### Gmail

1. Google Cloud Console → APIs & Services → enable the **Gmail API**.
2. OAuth consent screen: set it to External or Testing, and add your Gmail address as a **test user**.
3. Credentials → **OAuth client ID** (Web application). Add the redirect URI from the table above. Put the client ID and secret in `.env`.
4. In Gmail, create the label **Property Demo** (left sidebar → *Create new label*).
5. In the app, open Settings and click **Connect Gmail (read-only)**. The app requests `gmail.readonly` and `userinfo.email` only.

### QuickBooks

1. Go to developer.intuit.com and create an app with the **Accounting** scope. Add the redirect URI from the table under **Keys & credentials**.
2. Choose the environment deliberately:
   * `QUICKBOOKS_ENVIRONMENT=sandbox` uses the **Development** keys and a developer *sandbox company*.
   * `QUICKBOOKS_ENVIRONMENT=production` uses the **Production** keys and a real QuickBooks Online *test subscription*. Intuit requires HTTPS redirect URIs for production.
3. Set `QUICKBOOKS_EXPECTED_REALM_ID` to the test company's ID. In a sandbox company, find it under the gear icon → *Additional info*, or in the developer dashboard. **Bills cannot be created until this is set.** Every write first checks that the connected company is this one and re-reads its details from QuickBooks.
4. Restart the server, open Settings, click **Connect QuickBooks**, and confirm the page shows *verified test company*.
5. In Settings, add **vendor mappings** (vendor name as printed on bills → QuickBooks vendor) and **expense account mappings** (a keyword such as `utility` → expense account). You can also choose a vendor and account on each bill.

### OpenAI

Set `OPENAI_API_KEY` in the `.env` file on the server. Defaults: `gpt-4o` reads bills (PDFs are sent as `file` inputs and photos as `image_url` inputs), `gpt-4o-mini` interprets questions, and `whisper-1` transcribes speech. Without a key, bills show as *could not be read*, you can enter the values by hand, and questions use a built-in keyword interpreter.

### Todoist (optional)

Set `TODOIST_ENABLED=true`, `TODOIST_API_TOKEN` and `TODOIST_PROJECT_ID`.

## 7. Run the checks

```powershell
# Backend: unit, provider, workflow and startup tests (isolated temp database; never reads your .env)
cd backend; ..\venv\Scripts\python.exe -m pytest; cd ..

# Frontend: strict type check + production build, then component tests (Vitest)
cd frontend
npm run build
npm test

# Browser end-to-end workflow (Playwright). Starts a real backend on port 8765 with a temporary
# database and file store and deterministic TEST-ONLY providers (fake Gmail, QuickBooks and OpenAI).
npm run test:e2e
cd ..
```

The browser test signs in, imports the labeled vendor PDF, opens the original, reviews the fields, approves once, and checks that the test QuickBooks company holds exactly one unpaid bill with the correct PDF. It then uploads the same bytes under a different filename and checks for two arrivals with still one document and one bill. It asks what happened and compares the answer with the saved history. It also runs fixture B (unclear property, corrected without posting) and fixture C (a bill already entered in QuickBooks, linked without creating a new one).

## 8. How approval and recovery behave

* **Create unpaid bill** is the only action that writes to QuickBooks. Saving a correction never posts and never changes an existing QuickBooks bill. If you correct a bill that is already recorded, the app flags the difference for you to fix in QuickBooks.
* As soon as a bill has been read (and again when you change its vendor, invoice number, amount or date), the app searches QuickBooks for an existing bill, so a bill that is already entered goes straight to *Needs review*. If QuickBooks can't be reached, the bill is still stored and asks you to retry the check.
* Before every create, the app runs a complete, all-pages search of that vendor's bills. If any page fails, nothing is posted and you get a retry. A possible match must be linked or dismissed with a reason first. Dismissed matches keep their reason, and a newly found match blocks again.
* The approval, the company, the invoice claim and the request ID are committed **before** the request is sent. A second click, a second tab, or a second file for the same vendor invoice is refused by the database.
* If QuickBooks doesn't answer, the bill shows **QuickBooks Outcome Unknown** and offers **Check QuickBooks and resume**. That action searches QuickBooks for the bill tagged with this operation. It adopts the bill if found; otherwise it re-sends the *same* request ID, which Intuit de-duplicates.
* The bill ID is saved before the PDF upload starts. **Retry attachment** always works on that same bill. It marks the bill complete only when an attachment is proven to be this exact file, either by the SHA-256 note the app writes or by downloading the attachment and comparing its bytes.
* After a restart, any unfinished write is shown with one of these resume actions.

## 9. Live rehearsal runbook (your Gmail, QuickBooks test company, OpenAI key and phone)

The automated tests use test providers only. Run these steps yourself with the real accounts. Any step you can't run should be recorded as **unverified**.

1. **Make the fixture bills** (or use your own redacted bills): `.\venv\Scripts\python.exe backend\tests\make_fixtures.py C:\work\demo-fixtures` writes three PDFs.
   * A: Metro Power & Light, MPL-2026-0415, USD 142.50, 123 Main St
   * B: City Water Dept, no service address
   * C: Northside Electric Supply, NES-5531, USD 389.00

   In the QuickBooks test company, create vendors with those names (or map them in Settings). **Enter bill C by hand** in QuickBooks (vendor Northside Electric Supply, bill no. NES-5531, USD 389.00, date 2026-09-30). Note the number of bills per vendor before you start.
2. **Phone access over HTTPS.** A phone's `localhost` is the phone itself, so it needs a real HTTPS address. The simplest option is a tunnel: run `cloudflared tunnel --url http://localhost:8000` or `ngrok http 8000`. Then set `PUBLIC_ORIGIN=https://<your-tunnel-host>` in `.env`, add the tunnel's two callback URIs to Google and Intuit, and restart. Alternative on the local network: create a certificate with `mkcert`, install the mkcert root CA on the phone, and run `serve --host 0.0.0.0 --ssl-certfile cert.pem --ssl-keyfile key.pem`.
3. Start the app (section 5), sign in, and connect Gmail and QuickBooks in Settings. Check that Settings shows *verified test company*.
4. **Connection proof:** upload a spare test invoice that is not A, B or C. Approve it, open the bill in QuickBooks with the **Open bill in QuickBooks** link, and confirm the amount and the attached PDF.
5. **Email intake:** email A to your Gmail from another account, apply the **Property Demo** label, and leave an unrelated email unlabeled. Click **Import From Inbox** twice. A should appear once, with one email arrival, and the unrelated email should not appear.
6. **Accounting:** open A, check the fields beside the original, click **Review and approve**, check the company and values, then click **Create unpaid bill**. In QuickBooks there should be one unpaid bill with the PDF attached. Clicking again or reloading must not create another.
7. **Phone intake:** on the phone, upload the same PDF under another name. The result should be *already stored*, with **Arrivals (2)** and still one bill.
8. **Uncertainty:** upload B. The app should ask “Which property is this for?”. Correct it. The history should log your decision, and nothing should be posted.
9. **Existing record:** upload C. Without any click it should land in *Needs review* asking whether it is the same invoice as the manual bill, with the matching fields shown (**Check QuickBooks for existing bill** repeats the lookup on demand). Click **Link to this bill**. The number of bills in QuickBooks must not change.
10. **Retrieval and voice:** on the phone, open *Ask about a bill* (magnifier icon) and tap **🎤 Talk**. Ask “What happened to the power bill?”, edit the transcript if needed, tap **Ask**, then **Play answer**. Ask “Which property was it for?”. Then say “Change the property on this bill to Oak Street”, check the proposed change, and **Cancel** it or **Confirm** it. Deny microphone access once and check that typing still works. Repeat the microphone check on the laptop.
11. **Failure rehearsal (optional, careful):** start an approval and turn off the PC's network after the bill is created but before the attachment step finishes. Restart the app and click **Retry attachment**. It must resume on the same bill ID. Treat this as best-effort; the automated tests cover it with test providers.
12. **Access and persistence:** sign out, open a `/documents/<id>` link (it should redirect to sign-in), and try `/api/documents/<id>/file` (it should return 401). Restart the server, sign in, and check that the original, the corrections and the QuickBooks IDs are still there.

## 10. Data locations, backup and restore

`python backend\cli.py doctor` prints the data folder. It contains:

* `morning_assistant.db`, the SQLite database (`-wal` and `-shm` files may sit next to it while the app runs)
* `documents\`, the original files, stored once each and named by their SHA-256
* `emails\`, the raw labeled emails (`.eml`)
* `temp\`, temporary upload files

Back up with the server stopped (step 4 shows the command). To restore, stop the server, then run `Expand-Archive backup-<stamp>.zip -DestinationPath <data folder> -Force`. Always restore the database and `documents\` together.

To start over for a demo from the browser, click the red **Reset demo data** (bin) button next to Settings. It shows what will be removed, asks you to type `RESET`, saves `backup-<stamp>.zip` beside the data folder, then deletes every stored document, file and history entry, closes the Todoist tasks the app opened, and deletes the QuickBooks bills **this app created** (recognised by the memo it writes) with their attachments. Bills entered in QuickBooks by hand are never deleted, and connections, properties and mappings are kept. If QuickBooks refuses a deletion, the app's own data is left untouched.

To do the local part from the command line instead, stop the server and run `.\venv\Scripts\python.exe backend\cli.py reset-documents`. It saves `backend\backup-<stamp>.zip`, then deletes every stored document, file and history entry. It keeps the QuickBooks/Gmail connections, properties and mappings. Bills already in QuickBooks and tasks in Todoist are not removed.

## 11. Troubleshooting

* **“Database not found … run cli.py migrate”**: run `doctor` to see which database is expected, then `migrate`.
* **`redirect_uri_mismatch` (Google) or “redirect URI is invalid” (Intuit)**: register the exact URI that `doctor` prints, and browse using that same host.
* **“QUICKBOOKS_EXPECTED_REALM_ID is not set” / “not the configured test company”**: set the ID in `.env`, restart, then reconnect the right company.
* **Gmail label not found**: create the label exactly as `GMAIL_LABEL_NAME` (case doesn't matter).
* **Voice doesn't work on the phone**: it needs HTTPS and microphone permission. Typing always works.

## API overview

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/auth/login`, `/logout`, `/session` | POST/POST/GET | Owner session and CSRF token |
| `/api/documents` (`/needs-review`, `/ready-for-approval`) | GET | Lists |
| `/api/documents/{id}`, `/file`, `/history` | GET | Detail, original bytes, readable history |
| `/api/documents/upload` | POST | Upload (multipart, with `attempt_id`) |
| `/api/documents/{id}/update` | POST | Versioned correction (never posts) |
| `/api/documents/{id}/retry-extraction` | POST | Read again |
| `/api/documents/{id}/resolve-copy` | POST | Answer “same invoice as the stored bill?” (`same` links it as a copy, `different` keeps it separate) |
| `/api/gmail/authorize`, `/callback`, `/import`, `/status` | GET/GET/POST/GET | Gmail |
| `/api/quickbooks/authorize`, `/callback`, `/status`, `/reference-data` | GET | QuickBooks connection |
| `/api/quickbooks/{id}/check-duplicates`, `/duplicate-candidates`, `/resolve-duplicate` | POST/GET/POST | Duplicate checks |
| `/api/quickbooks/{id}/approve` | POST | **Create unpaid bill** |
| `/api/quickbooks/{id}/reconcile`, `/retry-attachment` | POST | Recovery |
| `/api/search`, `/api/transcribe` | POST | Assistant |
| `/api/admin/reset-demo` | GET/POST | Preview / run the demo reset (POST needs `confirm: "RESET"`) |
