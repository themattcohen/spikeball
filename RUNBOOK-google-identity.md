# Runbook: moving the Google identity behind the routine

## Status

As of 2026-09-09, the routine's Google identity stays as `mcohen@spikeball.com` for
now. This is a deliberate decision, not an oversight: the routine works correctly
under that identity, and moving it is a separate piece of work from getting the
routine running at all (see `UNBLOCK.md`). Use this runbook when you're ready to move
it to `casandra@spikeball.com` -- there is no deadline attached to that move.

## What this identity controls

Every Google API call the routine makes -- reading and writing the "Spikeball Finance
Data" Sheet, reading and writing the Drive state file, writing to the BigQuery dataset,
and sending the failure alert email -- runs as whichever Google account most recently
completed the OAuth consent that produced the refresh token currently stored in the
`SPIKEBALL_GCP_OAUTH_REFRESH_TOKEN` environment variable. That token was minted for
`mcohen@spikeball.com`. Moving it means having `casandra@spikeball.com` complete that
same consent instead, then swapping the resulting token into the environment. After
the swap, the failure alert also starts sending from her mailbox rather than the
owner's, since the alert is sent through the same Google identity.

The consent flow itself is `spike/routine/google_consent.py` in the repository. It
uses `SPIKEBALL_OAUTH_CLIENT_ID` and `SPIKEBALL_OAUTH_CLIENT_SECRET` -- an OAuth client
that belongs to Spikeball's own Google Cloud project, `spikeball-coding-automation`,
not to any consultant's account -- and requests four scopes: Drive, Cloud Platform
(BigQuery), Gmail send, and the account's own email address (used only to confirm
which identity completed consent). It does not touch NetSuite or Amazon; those use
separate, unrelated credentials that are not affected by this runbook.

## Prerequisites: grant access before running consent

`casandra@spikeball.com` needs the following access in place *before* she completes
the consent flow, or the routine will start failing with permission errors on its very
next run even though the token itself is valid:

1. **Editor** access on the Google Sheet "Spikeball Finance Data"
   (`SPIKEBALL_FINANCE_SHEET_ID`). Share it with her directly if she isn't already an
   editor.
2. **Editor** access on the Drive file that holds the pipeline's carry-forward state
   (`SPIKEBALL_DASH_STATE_FILE_ID`).
3. **BigQuery Data Editor** and **BigQuery Job User** roles on the Google Cloud
   project `spikeball-coding-automation`. Grant these in that project's IAM settings,
   not in BigQuery's own sharing dialog.

Confirm all three before continuing. Skipping any of them turns a working routine into
one that fails partway through a run, which is harder to diagnose after the fact than
checking three access grants up front.

## Running the consent flow

1. From a checkout of the repository (the same one the routine runs from, or a local
   clone with `SPIKEBALL_OAUTH_CLIENT_ID` and `SPIKEBALL_OAUTH_CLIENT_SECRET` exported
   as environment variables), run:
   ```bash
   python spike/routine/google_consent.py
   ```
2. It opens (or prints a link to) Google's consent screen. `casandra@spikeball.com`
   must be the account signed in when she approves it -- if the browser is signed in
   as a different Google account, sign out first or use an incognito/private window.
3. Approve all four requested scopes (Drive, Cloud Platform, Gmail send, account
   email). Declining any of them produces a token that will fail partway through a
   run.
4. The script prints the newly minted refresh token once, to the terminal only. It is
   not written to any file and not logged anywhere.

## Installing the new token

Copy the printed value directly into the `SPIKEBALL_GCP_OAUTH_REFRESH_TOKEN`
environment variable on the `Spikeball Finance` cloud environment
(`env_01XTN5CezsGWv8FTYZLEVn61`), replacing the existing value, then save. Never paste
the token anywhere else -- not into a chat message, a document, a commit, or any file
in the repository. Treat it exactly like a password: if it is ever pasted somewhere
other than that one environment-variable field, mint a fresh one and discard the
exposed value.

## Verifying it worked

Trigger the routine once (per `UNBLOCK.md` step 4) and confirm:

- The run completes with `NIGHTLY_OK` or `NIGHTLY_PARTIAL_OK`, not a Google API
  permission error.
- The Sheet and the Drive state file both show a new modification from
  `casandra@spikeball.com`, not `mcohen@spikeball.com` (check each file's version
  history / "last modified by").
- If you deliberately trigger a failure alert (or wait for one to occur naturally),
  confirm the email arrives from `casandra@spikeball.com`, not the owner's address.

## Rollback

If anything goes wrong after the swap, the previous refresh token is still valid
until it is explicitly revoked (consenting again does not revoke the old token). Ask
the owner, `mcohen@spikeball.com`, to run `spike/routine/google_consent.py` again
himself to mint a fresh token under his identity, and put that value back into
`SPIKEBALL_GCP_OAUTH_REFRESH_TOKEN`. The Sheet, Drive, and BigQuery access grants made
to `casandra@spikeball.com` above are harmless to leave in place either way.
