/**
 * Spikeball Finance Dashboard - refresh request web app.
 *
 * A single GET endpoint that records a request to re-pull the dashboard's
 * data. It appends a row to the `refresh_requests` tab so the nightly gate
 * (spike/routine/refresh_gate.py) can pick it up on its next scheduled check.
 * It never triggers a pull itself. See PRD-month-refresh.md Section 4 (data
 * contract) and Section 5 M3 (this endpoint's spec).
 */

var SPREADSHEET_ID = '1aLGh8fYVKGe-T08-1tZe1eTmWwmBRiPtS12ChmnjMHs';
var SHEET_NAME = 'refresh_requests';
var HEADER = ['requested_at_utc', 'requested_at_mt', 'source', 'user_agent', 'status'];
var LOCK_TIMEOUT_MS = 10000;
var THROTTLE_WINDOW_MS = 10 * 60 * 1000;
var MT_TIME_ZONE = 'America/Denver';

/**
 * Web app entry point. Records one refresh request per 10-minute window and
 * always returns a small HTML confirmation page. Never throws to the caller;
 * any failure is logged server-side and answered with a generic error page.
 */
function doGet(e) {
  var lock = LockService.getScriptLock();
  try {
    var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    lock.waitLock(LOCK_TIMEOUT_MS);
    try {
      var sheet = getOrCreateSheet_(ss);
      var lastRaw = getLastNonEmptyRequestedAtUtc_(sheet);
      var lastInstant = lastRaw ? parseRequestedAtUtc_(lastRaw) : null;
      if (lastRaw && !lastInstant) {
        console.error('refresh_requests: last requested_at_utc value does not parse as an ISO string: ' + lastRaw);
      }

      var now = new Date();
      if (lastInstant && (now.getTime() - lastInstant.getTime()) < THROTTLE_WINDOW_MS) {
        return buildPage_(
          'Refresh already requested',
          'A refresh was already requested at ' + formatMtTime_(lastInstant) + '; the next check honors it.'
        );
      }

      appendRequestRow_(sheet, now);
      return buildPage_(
        'Refresh requested',
        'Refresh requested at ' + formatMtTime_(now) + '. The dashboard republishes within the next hourly ' +
          'check (07:00 to 18:00 MT) or at 03:00 MT. Reload the dashboard after that; the As-of pill shows ' +
          'the new pull time.'
      );
    } finally {
      lock.releaseLock();
    }
  } catch (err) {
    console.error('refresh_request_webapp doGet failed: ' + (err && err.stack ? err.stack : String(err)));
    return buildPage_(
      'Refresh request error',
      'The refresh request could not be recorded. Try again in a minute.'
    );
  }
}

/**
 * Gets the refresh_requests tab, creating it and writing the header if it
 * does not exist yet. Idempotent: re-reads row 1 after any insert and only
 * (re)writes the header when it does not already match exactly. Never
 * appends a second header row.
 */
function getOrCreateSheet_(ss) {
  var sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(SHEET_NAME);
    sheet.getRange(1, 1, 1, HEADER.length).setValues([HEADER]);
  }

  var headerRange = sheet.getRange(1, 1, 1, HEADER.length);
  var currentHeader = headerRange.getValues()[0];
  if (!headerMatches_(currentHeader)) {
    headerRange.setValues([HEADER]);
  }

  return sheet;
}

function headerMatches_(currentHeader) {
  if (!currentHeader || currentHeader.length !== HEADER.length) {
    return false;
  }
  for (var i = 0; i < HEADER.length; i++) {
    if (currentHeader[i] !== HEADER[i]) {
      return false;
    }
  }
  return true;
}

/**
 * Scans column A from the bottom for the last non-empty requested_at_utc
 * cell. Returns the raw cell value (expected to be a string) or null when
 * there are no data rows yet.
 */
function getLastNonEmptyRequestedAtUtc_(sheet) {
  var lastRow = sheet.getLastRow();
  if (lastRow < 2) {
    return null;
  }
  var values = sheet.getRange(2, 1, lastRow - 1, 1).getValues();
  for (var i = values.length - 1; i >= 0; i--) {
    var raw = values[i][0];
    if (raw !== '' && raw !== null && typeof raw !== 'undefined') {
      return raw;
    }
  }
  return null;
}

/**
 * Parses a requested_at_utc cell value as an instant. Handles the expected
 * ISO string shape and, defensively, a legacy Date-typed cell. Returns null
 * when the value cannot be parsed.
 */
function parseRequestedAtUtc_(raw) {
  if (raw instanceof Date) {
    return isNaN(raw.getTime()) ? null : raw;
  }
  if (typeof raw === 'string' && raw) {
    var parsed = new Date(raw);
    if (!isNaN(parsed.getTime())) {
      return parsed;
    }
  }
  return null;
}

/**
 * Appends one request row as plain-text strings. The target row's first two
 * columns are forced to plain-text number format before the write so Sheets
 * never reinterprets the timestamp strings as Date values.
 */
function appendRequestRow_(sheet, now) {
  var newRowIndex = sheet.getLastRow() + 1;
  sheet.getRange(newRowIndex, 1, 1, 2).setNumberFormat('@');
  sheet.appendRow([
    formatUtcIso_(now),
    formatMtDateTime_(now),
    'dashboard',
    '', // Apps Script's doGet(e) does not expose the requesting browser's user agent; see README.
    'queued'
  ]);
}

/** YYYY-MM-DDTHH:MM:SSZ in UTC. */
function formatUtcIso_(date) {
  return Utilities.formatDate(date, 'GMT', "yyyy-MM-dd'T'HH:mm:ss'Z'");
}

/** YYYY-MM-DD HH:MM MT (America/Denver, 24-hour). */
function formatMtDateTime_(date) {
  return Utilities.formatDate(date, MT_TIME_ZONE, 'yyyy-MM-dd HH:mm') + ' MT';
}

/** HH:MM MT (America/Denver, 24-hour), for response copy. */
function formatMtTime_(date) {
  return Utilities.formatDate(date, MT_TIME_ZONE, 'HH:mm') + ' MT';
}

/**
 * Builds a minimal, inline-styled confirmation page. No script tags, no
 * external assets, nothing echoed from the request. The title carries a
 * per-request nonce so browsers never serve a replayed/cached copy.
 */
function buildPage_(titlePrefix, message) {
  var nonce = Utilities.getUuid();
  var title = escapeHtml_(titlePrefix) + ' &middot; ' + nonce;
  var html =
    '<!DOCTYPE html>' +
    '<html>' +
    '<head>' +
    '<meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    '<title>' + title + '</title>' +
    '</head>' +
    '<body style="margin:0;padding:48px 20px;background:#f5f5f5;' +
    'font-family:Arial,Helvetica,sans-serif;color:#1a1a1a;text-align:center;">' +
    '<div style="max-width:480px;margin:0 auto;background:#ffffff;border:1px solid #ddd;' +
    'border-radius:8px;padding:32px 28px;">' +
    '<p style="margin:0;font-size:16px;line-height:1.5;">' + escapeHtml_(message) + '</p>' +
    '</div>' +
    '</body>' +
    '</html>';
  return HtmlService.createHtmlOutput(html);
}

function escapeHtml_(text) {
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Private self-check for the orchestrator to run from the Apps Script
 * editor (Run > selfCheck_, then View > Logs / Execution log). Returns the
 * tab name, whether the header matches exactly, and the current data row
 * count (excluding the header row).
 */
function selfCheck_() {
  var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  var sheet = ss.getSheetByName(SHEET_NAME);
  var result = {
    tabName: SHEET_NAME,
    tabExists: !!sheet,
    headerOk: false,
    rowCount: 0
  };

  if (sheet) {
    var currentHeader = sheet.getRange(1, 1, 1, HEADER.length).getValues()[0];
    result.headerOk = headerMatches_(currentHeader);
    result.rowCount = Math.max(sheet.getLastRow() - 1, 0);
  }

  Logger.log(JSON.stringify(result));
  return result;
}
