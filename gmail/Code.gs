/**
 * Job Tracker — Gmail sync
 *
 * Runs inside your own Google account (script.google.com), every hour.
 * Finds application emails from the last 3 days, sends each one to your
 * tracker, and labels the thread "job-tracked" so it's never sent twice.
 *
 * Setup: Project Settings → Script properties → add
 *   API_URL  = your app_url + "ingest"   (e.g. https://abc123.execute-api.us-east-1.amazonaws.com/ingest)
 *   API_KEY  = terraform output -raw access_key
 * Then run syncApplications once (approve access), then run installTrigger once.
 */

var SEARCH =
  'newer_than:3d -label:job-tracked (' +
  '"thank you for applying" OR "thanks for applying" OR "application received" OR ' +
  '"received your application" OR "your application" OR "unfortunately" OR ' +
  '"not moving forward" OR "schedule an interview" OR "phone screen" OR "next steps")' +
  ' -category:promotions -category:social';

function syncApplications() {
  var props = PropertiesService.getScriptProperties();
  var url = props.getProperty('API_URL');
  var key = props.getProperty('API_KEY');
  if (!url || !key) throw new Error('Set API_URL and API_KEY in Project Settings → Script properties.');

  var label = GmailApp.getUserLabelByName('job-tracked') || GmailApp.createLabel('job-tracked');
  var threads = GmailApp.search(SEARCH, 0, 30);
  var counts = { sent: 0, ignored: 0, failed: 0 };

  threads.forEach(function (thread) {
    var ok = true;
    thread.getMessages().forEach(function (m) {
      var res = UrlFetchApp.fetch(url, {
        method: 'post',
        contentType: 'application/json',
        headers: { 'x-api-key': key },
        muteHttpExceptions: true,
        payload: JSON.stringify({
          message_id: m.getId(),
          from: m.getFrom(),
          subject: m.getSubject(),
          date: m.getDate().toISOString(),
          body: m.getPlainBody().slice(0, 3000)
        })
      });
      var code = res.getResponseCode();
      var out = res.getContentText().slice(0, 300);
      Logger.log(code + '  ' + m.getSubject().slice(0, 60) + '  ->  ' + out);
      if (code >= 200 && code < 300) {
        if (out.indexOf('"ignored"') >= 0) counts.ignored++; else counts.sent++;
      } else { counts.failed++; ok = false; }
    });
    // Label only when every message went through, so a failure is retried next hour.
    if (ok) thread.addLabel(label);
  });

  Logger.log('done: ' + JSON.stringify(counts));
}

function installTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'syncApplications') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('syncApplications').timeBased().everyHours(1).create();
  Logger.log('Hourly sync installed.');
}
