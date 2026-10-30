'use strict';
/*
 * Website monitor canary (CloudWatch Synthetics, Node.js + Puppeteer runtime).
 *
 * One canary per AWS Region. Each run first reads the site list from
 * config/sites.json in the central bucket (edited by admins in the Cloud Infra
 * Service Desk portal: Monitoring > Website Monitoring), visits every site, then
 * for each site:
 *   - writes results/<siteId>/<regionKey>.json to the central bucket
 *       (up/down, HTTP status code, response time, last success time, ...)
 *   - on success, writes screenshots/<siteId>/<regionKey>.jpg (latest successful access)
 *   - publishes an SNS email alert when a site has failed FAIL_THRESHOLD runs in a row,
 *       and again when it recovers (state is kept in the previous result JSON).
 * The run itself is marked failed if any site is down, so CloudWatch Synthetics
 * metrics (SuccessPercent) also reflect availability.
 */
const synthetics = require('@aws/synthetics-puppeteer');
const log = require('@aws/synthetics-logger');
const fs = require('fs');
const { S3Client, GetObjectCommand, PutObjectCommand } = require('@aws-sdk/client-s3');
const { SNSClient, PublishCommand } = require('@aws-sdk/client-sns');

const CFG = {
  regionKey: process.env.REGION_KEY,
  regionLabel: process.env.REGION_LABEL || process.env.REGION_KEY,
  bucket: process.env.CENTRAL_BUCKET,
  centralRegion: process.env.CENTRAL_REGION,
  topicArn: process.env.ALERT_TOPIC_ARN,
  // Consecutive failed runs (5 min apart) before a DOWN email is sent.
  failThreshold: parseInt(process.env.FAIL_THRESHOLD || '3', 10),
  // Some sites take 20-25 s to answer; give them time before calling it a failure.
  navTimeoutMs: parseInt(process.env.NAV_TIMEOUT_MS || '60000', 10),
  // A response slower than this is still UP, just flagged "slow" (never alerts).
  slowMs: parseInt(process.env.SLOW_MS || '15000', 10),
  // Stop starting new sites once this much of the run has elapsed (canary timeout is 240s).
  budgetMs: parseInt(process.env.RUN_BUDGET_MS || '225000', 10),
  // No navigation may run beyond this point of the run (canary timeout is 280 s).
  hardStopMs: parseInt(process.env.RUN_HARD_STOP_MS || '262000', 10),
};

const s3 = new S3Client({ region: CFG.centralRegion });
const sns = new SNSClient({ region: CFG.centralRegion });

const SITE_ID_RE = /^[a-z0-9][a-z0-9-]{0,39}$/;

function isHttpUrl(value) {
  try {
    const u = new URL(value);
    return u.protocol === 'http:' || u.protocol === 'https:';
  } catch (err) {
    return false;
  }
}

/** The monitored site list. Read on every run so portal edits apply on the next run. */
async function loadSites() {
  let cfg;
  try {
    const res = await s3.send(new GetObjectCommand({ Bucket: CFG.bucket, Key: 'config/sites.json' }));
    cfg = JSON.parse(await res.Body.transformToString());
  } catch (err) {
    throw new Error(`Cannot read config/sites.json from the central bucket: ${err.name}: ${err.message}`);
  }
  const sites = (Array.isArray(cfg.sites) ? cfg.sites : []).filter((s) => {
    const ok = s && SITE_ID_RE.test(String(s.id)) && isHttpUrl(s.url);
    if (!ok) log.warn(`Ignoring invalid site entry: ${JSON.stringify(s)}`);
    return ok;
  });
  return sites.map((s) => ({ id: s.id, name: String(s.name || s.id), url: s.url }));
}

async function readPrevious(key) {
  try {
    const res = await s3.send(new GetObjectCommand({ Bucket: CFG.bucket, Key: key }));
    return JSON.parse(await res.Body.transformToString());
  } catch (err) {
    if (err.name !== 'NoSuchKey') {
      log.warn(`Could not read previous state ${key}: ${err.name}`);
    }
    return null;
  }
}

async function putObject(key, body, contentType) {
  await s3.send(new PutObjectCommand({
    Bucket: CFG.bucket,
    Key: key,
    Body: body,
    ContentType: contentType,
    CacheControl: 'no-cache',
  }));
}

async function notify(subject, message) {
  if (!CFG.topicArn) return;
  try {
    await sns.send(new PublishCommand({
      TopicArn: CFG.topicArn,
      Subject: subject.replace(/[^\x20-\x7E]/g, '').slice(0, 100),
      Message: message,
    }));
  } catch (err) {
    log.error(`SNS publish failed: ${err.name}: ${err.message}`);
  }
}

async function visit(page, site, timeoutMs) {
  const started = Date.now();
  const out = { up: false, statusCode: null, responseTimeMs: null, finalUrl: null, error: null };
  try {
    const resp = await page.goto(site.url, { waitUntil: 'domcontentloaded', timeout: timeoutMs });
    out.responseTimeMs = Date.now() - started;
    out.statusCode = resp ? resp.status() : null;
    out.finalUrl = page.url();
    out.up = out.statusCode !== null && out.statusCode >= 200 && out.statusCode < 400;
    if (!out.up) out.error = `HTTP ${out.statusCode}`;
  } catch (err) {
    out.responseTimeMs = Date.now() - started;
    out.error = (err && err.name === 'TimeoutError')
      ? `No response within ${Math.round(timeoutMs / 1000)}s (timeout)`
      : String((err && err.message) || err).slice(0, 300);
  }
  return out;
}

async function checkSite(page, site, runStart) {
  const resultKey = `results/${site.id}/${CFG.regionKey}.json`;
  const shotKey = `screenshots/${site.id}/${CFG.regionKey}.jpg`;
  const prev = await readPrevious(resultKey);
  // Never let one slow site run past the canary's own timeout.
  const timeoutMs = Math.min(CFG.navTimeoutMs, CFG.hardStopMs - (Date.now() - runStart));
  if (timeoutMs < 10000) {
    throw new Error('Not enough run time left to check this site');
  }
  const visitResult = await visit(page, site, timeoutMs);
  const now = new Date().toISOString();

  let screenshotAt = prev && prev.screenshotAt ? prev.screenshotAt : null;
  if (visitResult.up) {
    try {
      // Let late resources settle a little so the screenshot is representative.
      await page.waitForNetworkIdle({ idleTime: 1000, timeout: 8000 }).catch(() => {});
      const shot = Buffer.from(await page.screenshot({ type: 'jpeg', quality: 70 }));
      fs.writeFileSync(`/tmp/${site.id}-${CFG.regionKey}.jpg`, shot); // also kept in the regional artifact bucket
      await putObject(shotKey, shot, 'image/jpeg');
      screenshotAt = now;
    } catch (err) {
      log.warn(`Screenshot failed for ${site.id}: ${err.message}`);
    }
  }

  const consecutiveFailures = visitResult.up ? 0 : ((prev && prev.consecutiveFailures) || 0) + 1;
  const wasAlerted = !!(prev && prev.alerted);
  let alerted = wasAlerted;

  if (!visitResult.up && consecutiveFailures >= CFG.failThreshold && !wasAlerted) {
    alerted = true;
    await notify(
      `[DOWN] ${site.name} from ${CFG.regionLabel}`,
      `Website DOWN\nSite: ${site.name}\nURL: ${site.url}\nProbe region: ${CFG.regionLabel}\n` +
      `Status code: ${visitResult.statusCode === null ? 'n/a' : visitResult.statusCode}\n` +
      `Error: ${visitResult.error}\nConsecutive failures: ${consecutiveFailures}\nTime (UTC): ${now}`
    );
  } else if (visitResult.up && wasAlerted) {
    alerted = false;
    await notify(
      `[RECOVERED] ${site.name} from ${CFG.regionLabel}`,
      `Website RECOVERED\nSite: ${site.name}\nURL: ${site.url}\nProbe region: ${CFG.regionLabel}\n` +
      `Status code: ${visitResult.statusCode}\nResponse time: ${visitResult.responseTimeMs} ms\nTime (UTC): ${now}`
    );
  }

  const result = {
    siteId: site.id,
    name: site.name,
    url: site.url,
    region: CFG.regionKey,
    regionLabel: CFG.regionLabel,
    checkedAt: now,
    up: visitResult.up,
    statusCode: visitResult.statusCode,
    responseTimeMs: visitResult.responseTimeMs,
    // Up but slow: shown in the portal, never alerts.
    slow: !!(visitResult.up && visitResult.responseTimeMs !== null && visitResult.responseTimeMs >= CFG.slowMs),
    finalUrl: visitResult.finalUrl,
    error: visitResult.error,
    consecutiveFailures,
    alerted,
    lastSuccessAt: visitResult.up ? now : (prev && prev.lastSuccessAt) || null,
    screenshotAt,
  };
  await putObject(resultKey, JSON.stringify(result), 'application/json');
  log.info(`${site.id} [${CFG.regionKey}] up=${result.up} status=${result.statusCode} time=${result.responseTimeMs}ms`);
  return result;
}

const monitor = async function () {
  if (!CFG.bucket || !CFG.regionKey || !CFG.centralRegion) {
    throw new Error('Missing CENTRAL_BUCKET / CENTRAL_REGION / REGION_KEY environment variables');
  }
  const runStart = Date.now();
  const sites = await loadSites();
  const page = await synthetics.getPage();
  await page.setViewport({ width: 1366, height: 768 });
  // Headless Chrome announces itself as "HeadlessChrome", which many WAF/bot rules
  // (including our own sites') block outright. Present as regular Chrome and keep an
  // identifying token so WAF rules can still recognise and allow-list the monitor.
  try {
    const baseUa = await page.browser().userAgent();
    await page.setUserAgent(`${baseUa.replace('HeadlessChrome', 'Chrome')} WebMonitor/1.0 (${CFG.regionKey})`);
  } catch (err) {
    log.warn(`Could not set user agent: ${err.message}`);
  }

  const down = [];
  for (const site of sites) {
    if (Date.now() - runStart > CFG.budgetMs) {
      log.warn(`Run time budget exceeded, skipping ${site.id}`);
      continue;
    }
    try {
      const r = await checkSite(page, site, runStart);
      if (!r.up) down.push(`${site.id} (${r.error})`);
    } catch (err) {
      // Infrastructure problem (e.g. S3 write failed) - do not report the site itself as down.
      log.error(`Check for ${site.id} could not complete: ${err.message}`);
      down.push(`${site.id} (monitor error: ${err.message})`);
    }
  }

  if (down.length > 0) {
    throw new Error(`Failed: ${down.join('; ')}`);
  }
  return `All ${sites.length} sites are up from ${CFG.regionLabel}`;
};

exports.handler = async () => {
  return await monitor();
};
