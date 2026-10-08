# Slator — Localization Request Readiness Inbox using Jev

A local inbox dashboard for enterprise translation intake. Steps = Read all currently unread Gmail or Outlook Inbox messages, identify translation requests, analyze supported source attachments, infer target languages and deadlines, and show brief completeness, capacity and clarification points alongside each email.

## Requirements

This code requires a JEV API key and read access to either a Gmail account or an Outlook account (steps explained below).
Data is sent to JEV / Typesafe.ai for analysis.

## Fresh installation

Python 3.10+ is required. From the extracted folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env
open -e .env
```

Replace `paste_your_typesafe_api_key_here` with your TypeSafe API key, save, then run `python3 app.py`. On Windows, use `.venv\Scripts\python.exe` instead of activating; copy `.env.example` to `.env` and edit it in Notepad. The app does not require Node.js.

## Connect Gmail (one-time setup)

This uses the Gmail API and a Desktop OAuth client, not your Gmail password.

1. In [Google Cloud Console](https://console.cloud.google.com/), create/select a project and enable the **Gmail API**.
2. Configure **Google Auth platform** branding/audience. For your Workspace organization you can use an Internal app when available. For External testing, add your own email address as a test user.
3. Add the read-only scope `https://www.googleapis.com/auth/gmail.readonly` to the app's data access settings.
4. Create an OAuth client with application type **Desktop app**.
5. Download the client JSON and save it as `gmail-credentials.json` beside `app.py`. This is a separate file from `.env` and the JEV key.
6. From the app folder, with the virtual environment active, run:

```bash
python3 connect.py gmail
```

7. Choose the intended account in the browser and authorize read-only access.
8. Start or return to `python3 app.py`, set `MAIL_PROVIDER=gmail` in `.env`, restart, then click **Run inbox check**.

An organization may restrict OAuth apps and require administrator approval. An External app left in testing can require periodic reauthorization; repeat `connect.py gmail` if needed. For distribution beyond your own approved users, follow Google's verification and data-use requirements.

Official setup guide: https://developers.google.com/workspace/gmail/api/quickstart/python

## Connect Outlook / Microsoft 365 (one-time setup)

This uses Microsoft Graph with delegated read-only mail access and interactive device sign-in. It does not require an Outlook password or client secret.

1. Open [Microsoft Entra admin center](https://entra.microsoft.com/) → **App registrations** → **New registration**.
2. Choose supported account types appropriate to your mailbox. For a company-only application, use your organization's tenant. Personal Outlook accounts need a registration supporting personal Microsoft accounts.
3. Copy the **Application (client) ID** into `.env` as `OUTLOOK_CLIENT_ID`.
4. For a company-only registration, set `OUTLOOK_TENANT` to the **Directory (tenant) ID**. Use `common` only if the registration supports the corresponding multiple-account audience.
5. Under **Authentication**, enable **Allow public client flows**. This permits device-code sign-in; no web redirect URI or client secret is needed for this flow.
6. Under **API permissions**, add **Microsoft Graph → Delegated permissions → Mail.Read** and **User.Read**. Obtain administrator consent if your organization requires it.
7. Run:

```bash
python3 connect.py outlook
```

8. Follow the Microsoft sign-in URL and code printed in Terminal, choose your account and authorize.
9. Start or return to `python3 app.py`, set `MAIL_PROVIDER=outlook` in `.env`, restart, then click **Run inbox check**.

If device-code authentication is blocked by your organization's policies, ask your administrator to approve an appropriate authentication method; do not disable those policies. This prototype targets the connected user's Inbox, not shared mailboxes, archives or arbitrary folders.

Official references:
- https://learn.microsoft.com/en-us/entra/msal/python/getting-started/acquiring-tokens
- https://learn.microsoft.com/en-us/graph/api/user-list-messages?view=graph-rest-1.0

## Sender-domain filtering and reanalysis (v3)

Configure the provider and required allowed sender domains in `.env`, then restart `python3 app.py`:

```dotenv
MAIL_PROVIDER=gmail
SENDER_DOMAINS=gmail.com,customer.com
```

Use `MAIL_PROVIDER=outlook` for Outlook. Replace these example domains with your actual allowed sender domains. Optional initial `@` is accepted. The dashboard cannot change these settings. Old domain settings saved through earlier versions of the UI are ignored. Keep your existing API key, credentials and private `.local/` folder when upgrading.

Filtering uses the domain of the parsed From address, case-insensitively and exactly. `gmail.com` does not match `notgmail.com`, `mail.gmail.com`, or a display name mentioning Gmail. Add a subdomain separately if needed. Other senders are excluded before JEV calls or separate attachment downloads; fetching the email from the provider can still include body/inline content. Excluded messages are not stored as new analysis rows. The run records its filter and an excluded-domain count. Retries obey the same filter. This is a workflow filter, not sender authentication, and it cannot distinguish clients from linguists who use the same domain.

Changing the filter takes effect on the next scan after restart, including older unread emails. Prior results remain in history. To rerun an already analyzed email, including the old `progit.pdf` request, select it in run history and click **Reanalyze this email**. Its sender must be allowed by the current filter. The app fetches the message and attachments again, updates its results, and preserves your capacity settings. This explicit reanalysis updates the stored result; it does not create a new arrival.

Large files are parsed locally and counted in full; JEV receives bounded excerpts, which are labeled as sampled. Partial extraction requires a verified total word count for a capacity verdict.

## Inbox workflow

1. Click **Run inbox check** to fetch all currently unread messages in the connected account's Inbox.
2. JEV identifies translation requests and selects target-language/locale and deadline candidates from the message.
3. For likely requests, the app retrieves supported attachments, extracts text, and asks JEV for document specialism/type and brief completeness.
4. Each row shows sender, received time, subject, specialism, brief score, deadline capacity and clarification points. Select a row for full details on the right.
5. Confirm inferred dates and languages, or adjust time/capacity. Changes are saved locally.
6. Use **Download result** or **Print / Save PDF** for a selected email. Nothing is sent back to the sender.

The score is **the percentage of five brief requirements explicitly stated**: audience, use, target locale, quality expectations and reference guidance. For example, 3/5 = 60%. It is calculated from JEV's completeness checks, not a model-generated quality score. Deadline feasibility, unreadable attachments and classification uncertainty are separate flags.

Language and date inference are suggestions, not facts silently accepted on the user's behalf. Completeness is evaluated against the original email; model-inferred fields do not retroactively make a missing brief requirement complete. A user-confirmed locale edit is rechecked against the brief and can update that item.

## Unread Inbox scans

- Every run checks all currently unread messages in Inbox from the configured domains, regardless of received date. There is no lookback window. `FIRST_RUN_DAYS` and old watermarks are ignored.
- Gmail uses both INBOX and UNREAD labels; Outlook filters Inbox with `isRead eq false`. All provider result pages are retrieved.
- New matching emails are analyzed. Previously completed analyses are reused while the email remains unread. Failed analyses are retried only if the message is still in the unread Inbox results.
- Marking a message read or moving it out of Inbox removes it from the latest view on the next scan. Its stored result remains available in history. Marking it unread again reuses its result.
- **Unread emails** counts matching messages displayed in the selected scan. **Translation requests** counts their analyzed requests. Newly analyzed, reused and retried counts are shown separately. All checked emails, including non-requests, are visible by default.
- Mailbox changes during a paginated scan may be reflected on the next run. This is a scan snapshot, not a continuously synchronized mailbox. If retrieval fails, the run is flagged; run again to refresh.
- Deduplication uses provider + account + message ID. Run history shows membership at each scan; request details reflect the latest local edits and reanalysis, not an immutable audit record.
- The app never marks messages read, archives them or sends replies. Start a scan with **Run inbox check**; no background schedule runs while the app is closed.

## Date and language inference

The received timestamp is converted to `MAIL_TIMEZONE` (Europe/Madrid by default), and that local date becomes the first working date. Weekend start dates are allowed, but capacity counts Monday–Friday only.

Code constructs bounded date candidates; JEV selects which one is the translation deadline. Supported expressions include ISO dates, slash/dot numeric dates, English month names, today/tomorrow, weekdays, and “in N days/working days.” Relative dates use the email's received date, never the current run date. Numeric dates use `DATE_ORDER=DMY` by default. Ambiguous dates, missing years and “next Friday” are flagged. Unsupported phrases such as a complex conditional deadline remain unresolved. A meeting date is not automatically treated as the translation deadline.

All inferred deadlines are provisional until verified in the detail panel. The calculator uses **end-of-day** dates. Time-of-day/timezone mentions are flagged; adjust availability/reserved time accordingly. English date expressions are supported in this version; deadlines written only in other languages may require manual entry.

JEV selects targets from the language/locale taxonomy in `email_inference.py`, including 31 common languages and selected regional variants. Multiple targets are supported; source languages should be excluded. Other or uncertain target languages are flagged for manual clarification. Customize the taxonomy for your buyers. General non-English JEV performance needs validation against your own data.

## PDFs and word counts

- A PDF with non-readable pages displays **“words on readable pages only”**, with the page numbers identified.
- No-text pages can be blank or scanned; the app does not claim they necessarily contain missing words.
- The count is explicitly marked partial and the deadline verdict is withheld. Displayed required days are labeled as the minimum based on readable text.
- Enter a **verified total source word count** to restore the scheduling verdict. The override must include all source documents, not just the missing pages.
- Fully unreadable, password-protected or malformed documents are listed as extraction issues in email intake; other readable documents and the brief can still be analyzed.
- OCR is not performed automatically. OCR scanned material or obtain the source files before relying on the schedule.
- CJK/Thai counting is flagged as unverified and also requires an override for scheduling.
- DOCX counts cover body paragraphs and tables, excluding headers, footers, comments and image text. PDF counts remain extraction estimates even when every page yields some text; visual text can still be missed.

## Capacity assumptions

- **One linguist per target language**, not one linguist translating all target languages simultaneously.
- 5,000 post-edited source words per full day by default; editable.
- Start and deadline dates inclusive; Monday–Friday minus supplied holidays.
- `available linguist-days = max(0, working days - reserved days) × availability percentage`.
- `required linguist-days = source word count ÷ daily rate`.
- A full working day is assumed on the email received date; adjust availability for a late arrival or a delayed actual start.
- Machine translation is assumed ready at the start; all source words need post-editing. No automatic TM/repetition discounts or complexity multiplier.
- Review, formatting, approval and delivery time must be included through reserved days.
- No readable source attachment means capacity is withheld until documents or a verified total are available. The email body is not silently counted as source material.
- Capacity-only edits are local. Changing/confirming a target-language field makes one JEV check for the updated locale completeness.

## Input limits and API use

Supported attachments: PDF, DOCX, UTF-8 TXT and Markdown, up to 10 documents per request. There is no application-enforced file-byte limit, combined-byte limit, expanded-DOCX-byte limit, or PDF page-count limit. Your email provider, network timeouts and available memory still impose practical constraints. Unsupported, linked, embedded or excess-count attachments are flagged and prevent a reliable full-count verdict without an override. Inline signature images are skipped. This version does not fetch documents from links to SharePoint, Google Drive or other repositories.

For each new email there is one JEV metadata/classification call. A likely translation request adds one batched document/brief call. Transient retries can add calls. There are no segment-by-segment calls and no generative LLM dependency.

Long emails are sampled to a 14,000-byte inference budget. Brief checks use a 6,000-byte excerpt; source attachment text shares an 18,000-byte budget across files. Long sources are explicitly marked as sampled, while word counts use full extracted text. Sampling may miss relevant sections. Original email text remains available for local review. The questions tell JEV to treat email content as data, but this is not a guarantee against prompt injection. Review important decisions.

Question types: JEV `choice` for specialism/document type, targets/locales and deadline candidates; `noul` for request relevance and completeness. Default thresholds remain >= 0.8 / <= 0.2 for completeness and 0.7 classification confidence. These prototype thresholds are not independently validated guarantees.

Endpoint and model: `https://api.typesafe.ai/v1/systemone`, pinned to `jev-1.13.0` by default. Use your TypeSafe key, not a third-party gateway key.

## Data and credentials

- Mail scopes are read-only: Gmail `gmail.readonly`; Microsoft Graph `Mail.Read` plus `User.Read` to identify the connected account. There are no send, delete, archive or mark-read operations.
- API keys stay in `.env` on the server; OAuth tokens stay under `.local/`. This folder and its SQLite database are private to the OS account through file permissions, but **not encrypted at rest**.
- Unlike v1's upload-only mode, inbox mode intentionally persists email bodies, extracted document text, inference/results and run history in `.local/inbox.sqlite3` to support review and retries. Binary attachments are not persisted by this app.
- Gmail's full message response may contain inline attachment bytes inside the locally stored provider message; handle the private data folder as sensitive mailbox data.
- Live scans send selected email text and attachment excerpts to TypeSafe. The interface and database are local; model processing is hosted. TypeSafe's retention and processing terms depend on your agreement.
- Result downloads omit raw email body and source text, but contain sender/subject, filenames, results and planning fields.
- Do not share `.env`, `.local/`, `gmail-credentials.json`, or any token file. The supplied ZIP excludes all of them.
- To stop access, revoke the app in your Google/Microsoft account settings. Deleting local tokens removes the saved local authorization, but does not itself revoke provider consent.
- This is a loopback-bound, single-user prototype, not a public internet service. The server validates Host and a per-process token for POST actions. Changing `MAIL_PROVIDER` in `.env` and restarting does not switch accounts; rerun `connect.py` to authorize another account. Its run history will be separate.

## Files

- `app.py`: local server, document extraction, JEV client, calculator, manual-upload route.
- `mail_clients.py`: read-only Gmail/Graph API adapters and attachment handling.
- `connect.py`: one-time OAuth authorization.
- `email_inference.py`: bounded deadline candidates and target-language questions.
- `inbox.py`: SQLite queue, unread Inbox snapshots, run history, scoring and adjustments.
- `static/`: dashboard, original manual interface, CSS and supplied official logo.
- `.env.example`: all configuration placeholders.
- `test_app.py`, `test_inbox.py`: automated regression tests.

## Verification and remaining validation

Run `python3 -m unittest -v` with requirements installed. The automated tests cover the original calculator plus relative dates/timezones, ambiguous deadlines, PDF partial-count gating, count overrides, no-attachment behavior, provider pagination, deduplication, failed-fetch handling, retry recovery and sample isolation, plus exact sender-domain filtering and a readable 301-page PDF larger than 10 MB. Earlier browser interaction tests covered email selection, search, narrow-screen layout and the manual sample. A second end-to-end browser check with mocked mail/JEV services covered inbox ingestion, progress, editable/persisted capacity, second-run deduplication and run-history selection.

**Live Gmail/Outlook authorization and live JEV analysis have not been tested with your account/key.** Provider schemas and OAuth flows follow current official documentation. Validate classification accuracy and routing on representative requests before depending on results for operational commitments.

TypeSafe references:
- https://docs.typesafe.ai/api
- https://docs.typesafe.ai/cookbooks/date_extraction_cookbook
- https://docs.typesafe.ai/model-jaggedness/jev-1.13
