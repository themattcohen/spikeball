/**
 * Spikeball Finance Dashboard - refresh request web app.
 *
 * A single GET endpoint that records a request to re-pull the dashboard's data. It
 * appends a row to the `refresh_requests` tab so the routine's gate
 * (spike/routine/refresh_gate.py) picks it up on its next scheduled check. It never
 * triggers a pull itself. See PRD-month-refresh.md Section 4 (data contract) and
 * Section 5 M3 (this endpoint's spec).
 *
 * Reader-facing copy (the confirmation page) is written as plain, numbered steps and
 * derives every time it quotes from SCHEDULE_UTC_HOURS below -- the routine's own cron
 * hours in UTC -- so the Mountain Time wording stays correct across daylight saving and
 * across the CUTOVER slot change without editing this file's prose.
 */

var SPREADSHEET_ID = '1aLGh8fYVKGe-T08-1tZe1eTmWwmBRiPtS12ChmnjMHs';
var SHEET_NAME = 'refresh_requests';
var HEADER = ['requested_at_utc', 'requested_at_mt', 'source', 'user_agent', 'status'];
var LOCK_TIMEOUT_MS = 10000;
var THROTTLE_WINDOW_MS = 10 * 60 * 1000;
var MT_TIME_ZONE = 'America/Denver';

/**
 * The UTC hours the refresh routine's cron fires on: `0 0,10,13-23 * * *`. Kept in UTC,
 * the cron's own frame, so the Mountain Time wording below stays correct across daylight
 * saving without editing this file. If the cron ever changes (see the handoff packet's
 * CUTOVER.md), change this list to match and redeploy; every time this page quotes then
 * follows automatically.
 */
var SCHEDULE_UTC_HOURS = [0, 10, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23];

/** Roughly how long a real pull takes, start to republished page. */
var TYPICAL_RUN_MINUTES = 15;

/**
 * Web app entry point. Records one refresh request per 10-minute window and always
 * returns a small HTML confirmation page. Never throws to the caller; any failure is
 * logged server-side and answered with a generic error page.
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
        console.error('refresh_requests: last requested_at_utc does not parse as ISO: ' + lastRaw);
      }

      var now = new Date();
      if (lastInstant && (now.getTime() - lastInstant.getTime()) < THROTTLE_WINDOW_MS) {
        return buildPage_(
          'Refresh already requested',
          'A refresh was already requested at ' + formatMtClock_(lastInstant) + '.',
          'You do not need to ask again - that request is still waiting and covers this one.',
          buildSteps_(now, lastInstant)
        );
      }

      appendRequestRow_(sheet, now);
      return buildPage_(
        'Refresh requested',
        'Refresh requested at ' + formatMtClock_(now) + '.',
        'Nothing else is needed from you. Here is what happens next.',
        buildSteps_(now, now)
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
 * Gets the refresh_requests tab, creating it and writing the header if it does not exist
 * yet. Idempotent: re-reads row 1 after any insert and only (re)writes the header when it
 * does not already match exactly. Never appends a second header row.
 */
function getOrCreateSheet_(ss) {
  var sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(SHEET_NAME);
    sheet.getRange(1, 1, 1, HEADER.length).setValues([HEADER]);
  }
  var headerRange = sheet.getRange(1, 1, 1, HEADER.length);
  if (!headerMatches_(headerRange.getValues()[0])) {
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
 * Scans column A from the bottom for the last non-empty requested_at_utc cell. Returns
 * the raw cell value (expected to be a string) or null when there are no data rows yet.
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
 * Parses a requested_at_utc cell value as an instant. Handles the expected ISO string and,
 * defensively, a legacy Date-typed cell. Returns null when it cannot be parsed.
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
 * Appends one request row as plain-text strings. The row's first two columns are forced to
 * plain-text number format before the write so Sheets never reinterprets the timestamp
 * strings as Date values.
 */
function appendRequestRow_(sheet, now) {
  var newRowIndex = sheet.getLastRow() + 1;
  sheet.getRange(newRowIndex, 1, 1, 2).setNumberFormat('@');
  sheet.appendRow([
    formatUtcIso_(now),
    formatMtDateTime_(now),
    'dashboard',
    '', // Apps Script's doGet(e) does not expose the requesting browser's user agent.
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

/** "1:04 PM MT" - the friendly clock form the confirmation page shows readers. */
function formatMtClock_(date) {
  return Utilities.formatDate(date, MT_TIME_ZONE, 'h:mm a') + ' MT';
}

/** "Thursday, September 10" in Mountain Time. */
function formatMtDate_(date) {
  return Utilities.formatDate(date, MT_TIME_ZONE, 'EEEE, MMMM d');
}

/** The Mountain Time clock hour (0-23) a given instant falls on. */
function mtHourOf_(date) {
  return parseInt(Utilities.formatDate(date, MT_TIME_ZONE, 'H'), 10);
}

/**
 * The first scheduled check strictly after `now`: walks forward hour by hour over the next
 * two days and returns the first instant whose UTC hour is in SCHEDULE_UTC_HOURS, at the
 * top of that hour.
 */
function nextCheckAfter_(now) {
  var probe = new Date(now.getTime());
  probe.setUTCMinutes(0, 0, 0);
  for (var i = 0; i <= 48; i++) {
    probe = new Date(probe.getTime() + 3600000);
    if (SCHEDULE_UTC_HOURS.indexOf(probe.getUTCHours()) !== -1) {
      return probe;
    }
  }
  return null;
}

/**
 * Plain-English Mountain Time description of the schedule, derived from SCHEDULE_UTC_HOURS
 * on `now`'s date so it reads correctly in both MDT and MST. Groups the MT hours into runs;
 * the longest run is the daytime window and any remaining hour is the overnight run.
 */
function describeScheduleMt_(now) {
  var base = new Date(now.getTime());
  base.setUTCMinutes(0, 0, 0);
  var hours = [];
  for (var i = 0; i < SCHEDULE_UTC_HOURS.length; i++) {
    var d = new Date(base.getTime());
    d.setUTCHours(SCHEDULE_UTC_HOURS[i]);
    hours.push(mtHourOf_(d));
  }
  hours.sort(function (a, b) { return a - b; });

  var runs = [];
  var run = [hours[0]];
  for (var j = 1; j < hours.length; j++) {
    if (hours[j] === hours[j - 1] + 1) {
      run.push(hours[j]);
    } else {
      runs.push(run);
      run = [hours[j]];
    }
  }
  runs.push(run);

  var longest = runs[0];
  for (var k = 1; k < runs.length; k++) {
    if (runs[k].length > longest.length) { longest = runs[k]; }
  }
  var others = [];
  for (var m = 0; m < runs.length; m++) {
    if (runs[m] !== longest) { others = others.concat(runs[m]); }
  }

  var text = 'every hour from ' + hourLabel_(longest[0]) +
    ' to ' + hourLabel_(longest[longest.length - 1]) + ' MT';
  if (others.length) {
    var labels = [];
    for (var n = 0; n < others.length; n++) { labels.push(hourLabel_(others[n])); }
    text += ', plus an overnight run at ' + labels.join(' and ') + ' MT';
  }
  return text;
}

/** 13 -> "1:00 PM", 7 -> "7:00 AM". */
function hourLabel_(hour24) {
  var suffix = hour24 < 12 ? 'AM' : 'PM';
  var h = hour24 % 12;
  if (h === 0) { h = 12; }
  return h + ':00 ' + suffix;
}

/** "about 16 minutes", "about 2 hours 30 minutes", "less than a minute". */
function minutesUntil_(from, to) {
  var mins = Math.round((to.getTime() - from.getTime()) / 60000);
  if (mins <= 0) { return 'less than a minute'; }
  if (mins === 1) { return 'about 1 minute'; }
  if (mins < 90) { return 'about ' + mins + ' minutes'; }
  var hours = Math.floor(mins / 60);
  var rest = mins % 60;
  var text = 'about ' + hours + (hours === 1 ? ' hour' : ' hours');
  if (rest) { text += ' ' + rest + (rest === 1 ? ' minute' : ' minutes'); }
  return text;
}

/**
 * The numbered steps both confirmation pages show. `queuedAt` is when the request that is
 * now waiting was recorded; `now` is this page load.
 */
function buildSteps_(now, queuedAt) {
  var next = nextCheckAfter_(now);
  var steps = [
    'It is now ' + formatMtClock_(now) + ' on ' + formatMtDate_(now) + '.',
    'The dashboard refreshes on its own ' + describeScheduleMt_(now) + '.'
  ];
  if (next) {
    var done = new Date(next.getTime() + TYPICAL_RUN_MINUTES * 60000);
    steps.push('Your request is in the queue. The next refresh starts shortly after ' +
      formatMtClock_(next) + ' (' + minutesUntil_(now, next) + ' from now).');
    steps.push('Pulling the data and rebuilding the page takes about ' +
      TYPICAL_RUN_MINUTES + ' minutes.');
    steps.push('Reload the dashboard after about ' + formatMtClock_(done) +
      '. The "As of" line at the top of the page will show the new time.');
  } else {
    steps.push('Your request is in the queue and the next scheduled refresh will pick it up.');
    steps.push('Reload the dashboard afterwards; the "As of" line at the top will show the new time.');
  }
  return steps;
}

/**
 * Builds a minimal, inline-styled confirmation page. No script tags, no external assets,
 * nothing echoed from the request. The title carries a per-request nonce so browsers never
 * serve a replayed/cached copy.
 */
function buildPage_(titlePrefix, headline, subhead, steps) {
  var nonce = Utilities.getUuid();
  var title = escapeHtml_(titlePrefix) + ' &middot; ' + nonce;

  var items = '';
  var list = steps || [];
  for (var i = 0; i < list.length; i++) {
    items += '<li style="margin:0 0 12px 0;padding-left:6px;">' + escapeHtml_(list[i]) + '</li>';
  }
  var listHtml = items
    ? '<ol style="margin:20px 0 0 0;padding-left:22px;font-size:15px;line-height:1.55;">' + items + '</ol>'
    : '';

  var html =
    '<!DOCTYPE html>' +
    '<html>' +
    '<head>' +
    '<meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    '<title>' + title + '</title>' +
    '</head>' +
    '<body style="margin:0;padding:48px 20px;background:#f5f5f5;' +
    'font-family:Arial,Helvetica,sans-serif;color:#1a1a1a;">' +
    '<div style="max-width:520px;margin:0 auto;background:#ffffff;border:1px solid #ddd;' +
    'border-radius:8px;padding:32px 28px;text-align:left;">' +
    '<p style="margin:0;font-size:19px;font-weight:bold;line-height:1.35;">' +
    escapeHtml_(headline) + '</p>' +
    (subhead
      ? '<p style="margin:10px 0 0 0;font-size:15px;line-height:1.5;color:#444;">' + escapeHtml_(subhead) + '</p>'
      : '') +
    listHtml +
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
 * Private self-check to run from the Apps Script editor (Run > selfCheck_, then View >
 * Logs). Returns the tab name, whether the header matches exactly, and the current data
 * row count (excluding the header row).
 */
function selfCheck_() {
  var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  var sheet = ss.getSheetByName(SHEET_NAME);
  var result = { tabName: SHEET_NAME, tabExists: !!sheet, headerOk: false, rowCount: 0 };
  if (sheet) {
    result.headerOk = headerMatches_(sheet.getRange(1, 1, 1, HEADER.length).getValues()[0]);
    result.rowCount = Math.max(sheet.getLastRow() - 1, 0);
  }
  Logger.log(JSON.stringify(result));
  return result;
}
