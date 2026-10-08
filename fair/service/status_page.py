"""A read-only status page for the FAIR service.

One static document. It holds no data: the browser asks the service's existing
JSON endpoints for everything it shows, with a client key the operator types
in, so the page adds nothing a client key could not already read and nothing at
all without one. It only ever sends GET requests.

It is kept as a string in a module rather than as a packaged file so that an
install can never ship the service without it, and so that nothing on the page
comes from anywhere but this file: no fonts, images or scripts are loaded, and
the Content-Security-Policy below pins the one stylesheet and the one script by
hash. The client key lives in a script variable and is gone on reload.
"""

import base64
import hashlib

_STYLE = r"""
:root {
  color-scheme: light dark;
  --ground: #eef1ee;
  --surface: #fbfcfa;
  --ink: #17231f;
  --muted: #5c6b64;
  --line: #c9d2cb;
  --ok: #1f7a4d;
  --wait: #a45f00;
  --out: #b3261e;
  --idle: #6b7780;
  --serif: "Sitka Display", "Sitka Heading", "Iowan Old Style", "Palatino Linotype",
    Georgia, serif;
  --sans: "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, sans-serif;
  --mono: "Cascadia Mono", Consolas, ui-monospace, monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ground: #121a17;
    --surface: #18221e;
    --ink: #e4ebe6;
    --muted: #93a29a;
    --line: #2b3833;
    --ok: #4cc38a;
    --wait: #f0a93b;
    --out: #f2726b;
    --idle: #8794a0;
  }
}
* { box-sizing: border-box; }
html { background: var(--ground); }
body {
  margin: 0 auto;
  max-width: 68rem;
  padding: 1.5rem 1.25rem 4rem;
  background: var(--ground);
  color: var(--ink);
  font: 1rem/1.5 var(--sans);
  font-variant-numeric: tabular-nums;
}
header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem 1.5rem;
  padding-bottom: 1rem;
  border-bottom: 1px solid var(--line);
}
h1 { margin: 0; font: 600 1rem/1.5 var(--sans); }
form { display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem; margin: 0; }
label { color: var(--muted); font-size: 0.875rem; }
input, button {
  font: inherit;
  font-size: 0.875rem;
  color: var(--ink);
  border: 1px solid var(--line);
  border-radius: 0.375rem;
  padding: 0.375rem 0.625rem;
  background: var(--surface);
}
input { width: 16rem; max-width: 100%; font-family: var(--mono); }
button { cursor: pointer; }
:focus-visible { outline: 2px solid var(--ink); outline-offset: 2px; }
[hidden] { display: none !important; }
#summary { padding: 2.5rem 0 2rem; }
#headline {
  margin: 0;
  max-width: 22ch;
  font: 400 clamp(2rem, 5.2vw, 3.5rem)/1.08 var(--serif);
  letter-spacing: -0.01em;
  text-wrap: balance;
}
#detail { margin: 1rem 0 0; max-width: 62ch; font-size: 1.0625rem; color: var(--muted); }
#detail strong { color: var(--ink); font-weight: 600; }
#detail .soon { color: var(--out); font-weight: 600; }
#stamp { margin: 0.75rem 0 0; font-size: 0.875rem; color: var(--muted); }
#stamp.stale { color: var(--out); }
h2 { margin: 2.5rem 0 0.5rem; font: 600 1.0625rem/1.4 var(--sans); }
table { width: 100%; border-collapse: collapse; background: var(--surface); }
th, td {
  padding: 0.75rem 0.875rem;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}
th { font-weight: 600; font-size: 0.875rem; color: var(--muted); }
tbody tr:last-child td { border-bottom: 0; }
.id { font-family: var(--mono); font-size: 0.9375rem; overflow-wrap: anywhere; }
.sub { display: block; font-size: 0.875rem; color: var(--muted); }
.state { display: flex; gap: 0.5rem; align-items: baseline; }
.lamp {
  flex: none;
  width: 0.625rem;
  height: 0.625rem;
  border-radius: 50%;
  background: var(--idle);
  transform: translateY(-0.0625rem);
}
.ok .lamp { background: var(--ok); }
.wait .lamp { background: var(--wait); }
.out .lamp { background: var(--out); }
.wait .word { color: var(--wait); }
.out .word { color: var(--out); }
.word { font-weight: 600; }
.bar {
  width: 100%;
  max-width: 9rem;
  height: 0.25rem;
  margin-top: 0.375rem;
  background: var(--line);
}
.bar span { display: block; height: 100%; background: var(--ink); }
.models { margin: 0; padding: 0; list-style: none; }
.resting { color: var(--wait); }
.note { margin: 0.5rem 0 0; max-width: 62ch; color: var(--muted); }
@media (max-width: 46rem) {
  table, tbody, tr, td { display: block; }
  thead { position: absolute; left: -100rem; }
  tr { padding: 0.5rem 0; border-bottom: 1px solid var(--line); }
  tbody tr:last-child { border-bottom: 0; }
  td { border: 0; padding: 0.25rem 0.875rem; }
  td[data-label]::before {
    content: attr(data-label);
    display: block;
    font-size: 0.8125rem;
    color: var(--muted);
  }
}
"""

_SCRIPT = r"""
(() => {
  "use strict";

  const POLL_MS = 15000;
  const DAY_MS = 86400000;
  const STATES = {
    ACTIVE: ["ok", "Taking requests", "is taking requests"],
    QUOTA_PRESSURE: ["wait", "Running low on allowance", "is running low on allowance"],
    THROTTLED: ["wait", "Waiting out a rate limit", "is waiting out a rate limit"],
    QUOTA_EXHAUSTED: ["out", "Out of allowance", "has used its allowance"],
    DEGRADED: ["wait", "Degraded", "is degraded"],
    OUTAGE: ["out", "Not answering", "is not answering"],
    DISABLED: ["out", "Switched off", "is switched off"],
    TERMS_REVIEW: ["out", "Held for a terms review", "is held for a terms review"],
    REVIEW_EXPIRED: ["out", "Review expired", "needs its review renewed"],
    SECURITY_BLOCKED: ["out", "Blocked until you review it", "is blocked until you review it"],
  };
  const ACCESS = {
    FREE_RECURRING: "free plan",
    FREE_DYNAMIC: "free models",
    FREE_LOCAL: "this computer",
  };
  const COUNTS = ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight",
    "Nine", "Ten", "Eleven", "Twelve"];

  const $ = (id) => document.getElementById(id);
  let key = "";
  let lastGood = 0;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function count(n) {
    return n >= 0 && n < COUNTS.length ? COUNTS[n] : String(n);
  }

  function clock(ms) {
    return new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  function day(ms) {
    return new Date(ms).toLocaleDateString([], { day: "numeric", month: "short" });
  }

  function routable(provider) {
    return provider.status === "ACTIVE" || provider.status === "QUOTA_PRESSURE";
  }

  async function read(path, withKey) {
    const headers = withKey ? { Authorization: "Bearer " + key } : {};
    const response = await fetch(path, { method: "GET", headers, cache: "no-store" });
    let body = null;
    try {
      body = await response.json();
    } catch (error) {
      body = null;
    }
    return { status: response.status, body };
  }

  function headline(total, ready, stopped) {
    if (stopped) return "FAIR is stopped.";
    if (!total) return "FAIR has no providers loaded.";
    const noun = total === 1 ? "provider" : "providers";
    if (ready === total) {
      return total === 1
        ? "The one provider can take requests."
        : "All " + count(total).toLowerCase() + " providers can take requests.";
    }
    if (!ready) {
      return total === 1
        ? "The one provider cannot take requests right now."
        : "No provider can take requests right now.";
    }
    return count(ready) + " of " + count(total).toLowerCase() + " " + noun +
      " can take requests.";
  }

  function reviewLine(providers, now) {
    const dates = providers
      .map((provider) => Date.parse(provider.review_expires_at || ""))
      .filter((value) => Number.isFinite(value));
    if (!dates.length) return null;
    const soonest = Math.min(...dates);
    const days = Math.ceil((soonest - now) / DAY_MS);
    if (days <= 0) {
      return el("span", "soon", "A provider review has run out. Renew it to bring that " +
        "provider back.");
    }
    const text = "The next provider review runs out in " + days +
      (days === 1 ? " day" : " days") + ", on " + day(soonest) + ".";
    return el("span", days <= 7 ? "soon" : "", text);
  }

  function showSummary(health, providers) {
    const total = providers ? providers.length : health.providers || 0;
    const ready = providers ? providers.filter(routable).length : health.routable_providers || 0;
    $("headline").textContent = headline(total, ready, health.status === "stopped");
    const detail = $("detail");
    detail.replaceChildren();
    if (!providers) {
      detail.append("Enter a FAIR client key to see each provider, what it has left " +
        "and when its review runs out.");
      return;
    }
    const down = providers.filter((provider) => !routable(provider));
    down.slice(0, 3).forEach((provider) => {
      const known = STATES[provider.status];
      detail.append(el("strong", "", provider.provider_id), " " +
        (known ? known[2] : "reports " + provider.status) + ". ");
    });
    if (down.length > 3) {
      const more = down.length - 3;
      detail.append(count(more) + " more " + (more === 1 ? "is" : "are") + " out too. ");
    }
    const review = reviewLine(providers, Date.now());
    if (review) detail.append(review);
  }

  function stateCell(provider) {
    const known = STATES[provider.status] || ["", String(provider.status), ""];
    const cell = el("td");
    cell.dataset.label = "State";
    const row = el("div", "state " + known[0]);
    row.append(el("span", "lamp"), el("span", "word", known[1]));
    cell.append(row, el("span", "sub id", provider.status));
    return cell;
  }

  function leftCell(provider) {
    const cell = el("td");
    cell.dataset.label = "Requests left";
    const left = provider.quota_remaining;
    const limit = provider.request_limit;
    if (typeof left !== "number") {
      cell.append(el("span", "sub", "No request cap is counted"));
      return cell;
    }
    if (typeof limit !== "number" || limit <= 0) {
      cell.append(left.toLocaleString());
      return cell;
    }
    cell.append(left.toLocaleString() + " of " + limit.toLocaleString());
    const bar = el("div", "bar");
    const fill = el("span");
    fill.style.width = Math.max(0, Math.min(100, (left / limit) * 100)) + "%";
    bar.append(fill);
    cell.append(bar);
    return cell;
  }

  function modelsCell(provider) {
    const cell = el("td");
    cell.dataset.label = "Models";
    const list = el("ul", "models");
    const resting = provider.benched_models || {};
    (provider.models || []).forEach((model) => {
      const item = el("li");
      item.append(el("span", "id", model));
      const until = resting[model];
      if (typeof until === "number" && until * 1000 > Date.now()) {
        item.append(el("span", "sub resting", "Resting until " + clock(until * 1000)));
      }
      list.append(item);
    });
    cell.append(list);
    return cell;
  }

  function reviewCell(provider, now) {
    const cell = el("td");
    cell.dataset.label = "Review";
    const expires = Date.parse(provider.review_expires_at || "");
    if (!Number.isFinite(expires)) {
      cell.append(el("span", "sub", "None needed"));
      return cell;
    }
    const days = Math.ceil((expires - now) / DAY_MS);
    if (days <= 0) {
      cell.append(el("span", "word", "Ran out " + day(expires)));
      cell.className = "out";
      return cell;
    }
    cell.append(days + (days === 1 ? " day left" : " days left"), el("span", "sub",
      "Runs out " + day(expires)));
    return cell;
  }

  function showProviders(providers) {
    const body = $("providers-body");
    const now = Date.now();
    body.replaceChildren();
    providers.forEach((provider) => {
      const row = el("tr");
      const name = el("td");
      name.dataset.label = "Provider";
      name.append(el("span", "id", provider.provider_id),
        el("span", "sub", ACCESS[provider.access_class] || provider.access_class));
      row.append(name, stateCell(provider), leftCell(provider), modelsCell(provider),
        reviewCell(provider, now));
      body.append(row);
    });
    $("providers").hidden = !providers.length;
  }

  function showSkipped(skipped) {
    const body = $("skipped-body");
    const names = Object.keys(skipped || {}).sort();
    body.replaceChildren();
    names.forEach((name) => {
      const row = el("tr");
      const id = el("td");
      id.dataset.label = "Provider";
      id.append(el("span", "id", name));
      const why = el("td", "", skipped[name]);
      why.dataset.label = "Why";
      row.append(id, why);
      body.append(row);
    });
    $("skipped").hidden = !names.length;
  }

  function showQuota(quota) {
    const body = $("quota-body");
    const note = $("quota-note");
    body.replaceChildren();
    $("quota").hidden = false;
    if (!quota || !quota.shared) {
      $("quota-title").textContent = "Quota sharing";
      $("quota-table").hidden = true;
      note.textContent = "Off. Requests are counted inside this FAIR service only, so " +
        "other applications using the same accounts are not included.";
      return;
    }
    $("quota-title").textContent = "Shared quota";
    // A pool nothing has been counted against, and that has no cap, says nothing.
    const pools = (quota.pools || []).filter((pool) =>
      pool.used > 0 || pool.exhausted || typeof pool.request_limit === "number");
    $("quota-table").hidden = !pools.length;
    note.textContent = quota.ledger_available === false
      ? "The shared quota file cannot be read right now, so FAIR is holding requests " +
        "rather than guessing."
      : pools.length
        ? "Counted across every application that shares this quota file."
        : "No requests have been counted in the shared quota file yet.";
    pools.forEach((pool) => {
      const row = el("tr");
      const id = el("td");
      id.dataset.label = "Account pool";
      id.append(el("span", "id", pool.pool_id));
      const used = el("td");
      used.dataset.label = "Used";
      used.append(typeof pool.request_limit === "number"
        ? pool.used.toLocaleString() + " of " + pool.request_limit.toLocaleString()
        : pool.used.toLocaleString());
      if (pool.exhausted) used.append(el("span", "sub resting", "Spent until it resets"));
      const resets = el("td");
      resets.dataset.label = "Resets";
      resets.append(typeof pool.reset_at === "number"
        ? day(pool.reset_at * 1000) + ", " + clock(pool.reset_at * 1000)
        : el("span", "sub", "Not started"));
      const apps = el("td");
      apps.dataset.label = "By application";
      const list = el("ul", "models");
      const names = Object.keys(pool.applications || {}).sort();
      names.forEach((name) => {
        const item = el("li");
        item.append(el("span", "id", name), " " + pool.applications[name].toLocaleString());
        list.append(item);
      });
      apps.append(names.length ? list : el("span", "sub", "None yet"));
      row.append(id, used, resets, apps);
      body.append(row);
    });
  }

  function stamp(ok, message) {
    const node = $("stamp");
    if (ok) {
      lastGood = Date.now();
      node.className = "";
      node.textContent = "Updated " + new Date(lastGood).toLocaleTimeString() +
        ". Refreshes every 15 seconds.";
      return;
    }
    node.className = "stale";
    node.textContent = message + (lastGood
      ? " Showing what FAIR reported at " + new Date(lastGood).toLocaleTimeString() + "."
      : "");
  }

  function signedOut(message) {
    key = "";
    $("key").value = "";
    $("key-form").hidden = false;
    $("forget").hidden = true;
    $("key-error").textContent = message || "";
    ["providers", "skipped", "quota"].forEach((id) => { $(id).hidden = true; });
  }

  async function refresh() {
    let health;
    try {
      health = (await read("/health", false)).body;
    } catch (error) {
      health = null;
    }
    if (!health) {
      stamp(false, "FAIR is not answering.");
      return;
    }
    if (!key) {
      showSummary(health, null);
      stamp(true);
      return;
    }
    let listed;
    let quota;
    try {
      [listed, quota] = await Promise.all([
        read("/v1/fair/providers", true),
        read("/v1/fair/quota", true),
      ]);
    } catch (error) {
      stamp(false, "FAIR stopped answering part-way through.");
      return;
    }
    if (listed.status === 401) {
      signedOut("FAIR did not accept that key.");
      showSummary(health, null);
      stamp(true);
      return;
    }
    if (listed.status !== 200 || !listed.body) {
      stamp(false, "FAIR could not list its providers.");
      return;
    }
    $("key-form").hidden = true;
    $("forget").hidden = false;
    $("key-error").textContent = "";
    showSummary(health, listed.body.providers || []);
    showProviders(listed.body.providers || []);
    showSkipped(listed.body.skipped);
    showQuota(quota.status === 200 ? quota.body : null);
    stamp(true);
  }

  $("key-form").addEventListener("submit", (event) => {
    event.preventDefault();
    key = $("key").value.trim();
    $("key").value = "";
    refresh();
  });
  $("forget").addEventListener("click", () => {
    signedOut("");
    refresh();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh();
  });
  window.setInterval(() => {
    if (!document.hidden) refresh();
  }, POLL_MS);
  refresh();
})();
"""

_BODY = """<header>
<h1>FAIR status</h1>
<form id="key-form" autocomplete="off">
<label for="key">Client key</label>
<input id="key" type="password" autocomplete="off" spellcheck="false" required>
<button type="submit">Show providers</button>
<span id="key-error" class="sub" role="alert"></span>
</form>
<button id="forget" type="button" hidden>Forget key</button>
</header>
<main>
<section id="summary" aria-live="polite">
<p id="headline">Asking FAIR how it is doing.</p>
<p id="detail"></p>
<p id="stamp"></p>
</section>
<section id="providers" hidden>
<h2>Providers</h2>
<table>
<thead><tr>
<th scope="col">Provider</th><th scope="col">State</th><th scope="col">Requests left</th>
<th scope="col">Models</th><th scope="col">Review</th>
</tr></thead>
<tbody id="providers-body"></tbody>
</table>
</section>
<section id="skipped" hidden>
<h2>Configured but not loaded</h2>
<table>
<thead><tr><th scope="col">Provider</th><th scope="col">Why</th></tr></thead>
<tbody id="skipped-body"></tbody>
</table>
</section>
<section id="quota" hidden>
<h2 id="quota-title">Shared quota</h2>
<p id="quota-note" class="note"></p>
<table id="quota-table" hidden>
<thead><tr>
<th scope="col">Account pool</th><th scope="col">Used</th><th scope="col">Resets</th>
<th scope="col">By application</th>
</tr></thead>
<tbody id="quota-body"></tbody>
</table>
</section>
</main>
<noscript><p class="note">This page needs JavaScript to ask FAIR for its status.</p></noscript>"""

STATUS_PAGE = (
    '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    '<meta name="robots" content="noindex">\n<title>FAIR status</title>\n'
    f"<style>{_STYLE}</style>\n</head>\n<body>\n{_BODY}\n"
    f"<script>{_SCRIPT}</script>\n</body>\n</html>\n"
)


def _pin(source):
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


STATUS_HEADERS = {
    # Nothing loads from anywhere, the page may only talk to the service that
    # served it, and the only script and stylesheet that run are the ones above.
    "Content-Security-Policy": (
        "default-src 'none'; "
        f"script-src {_pin(_SCRIPT)}; "
        f"style-src {_pin(_STYLE)}; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
