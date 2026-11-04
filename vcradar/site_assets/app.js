"use strict";
const W = {};
const useWording = data => Object.assign(W, data.wording || {});
const defaultFilters = () => ({period:"all", tag:"all", round:"all", status:"all", backfill:false, unsentOnly:false});
function activeFilterCount(filters) {
  return Number(filters.period !== "all") + Number(filters.tag !== "all") +
    Number(filters.round !== "all") + Number(filters.status !== "all") + Number(!!filters.backfill);
}
function filtersForRoute(hash, previous, filters) {
  if (hash !== previous && hash === "#backfill") Object.assign(filters, defaultFilters(), {unsentOnly:true});
  if (hash !== previous && hash !== "#backfill") filters.unsentOnly = false;
  return filters;
}

function selectEvents(data, filters) {
  const allowed = new Set(data.listing);
  return data.events.filter(e => {
    if (!allowed.has(e.id) || e.out_of_scope || e.filter_reasons?.length) return false;
    if (e.baseline_unpushed) return false;
    if (e.supplement_unsent && (filters.period !== "all" || filters.status !== "all")) return false;
    if (filters.unsentOnly && !(e.supplement_unsent && e.backfill_rank > 10)) return false;
    if (filters.period === "week" && !data.week.event_ids.includes(e.id)) return false;
    if (filters.period === "30" && (!e.push_date || e.push_date < data.meta.recent_cutoff || e.push_date > data.meta.today)) return false;
    if (filters.period.startsWith("date:") && e.push_date !== filters.period.slice(5)) return false;
    return (filters.tag === "all" || e.tags.includes(filters.tag)) &&
      (filters.round === "all" || e.round === filters.round) &&
      (filters.status === "all" || (filters.status === "backfill" ? !!e.backfill && !e.supplement_unsent :
        filters.status === W.admitted || filters.status === "已纳入" ? (e.admitted ?? (!e.supplement_unsent && (e.company_status || e.status) === "已纳入")) : e.status === filters.status)) &&
      (!filters.backfill || !!e.backfill && !e.supplement_unsent);
  }).sort((a, b) => (b.push_date || "9999").localeCompare(a.push_date || "9999") ||
    Number(!!a.baseline_unpushed) - Number(!!b.baseline_unpushed) ||
    data.listing.indexOf(a.id) - data.listing.indexOf(b.id));
}

const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
const icon = name => `<svg class="icon" aria-hidden="true" focusable="false"><use href="./icons.svg#i-${name}"></use></svg>`;
function external(url, label, className = "", arrow = false) {
  try {
    const parsed = new URL(url);
    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) return esc(label);
  } catch (_) { return esc(label); }
  return `<a${className ? ` class="${esc(className)}"` : ""} href="${esc(url)}" target="_blank" rel="noopener nofollow">${esc(label)}${arrow ? icon("ext") : ""}</a>`;
}
function linkedText(text, entities, className = "") {
  const valid = entities.filter(e => e.name && e.url).sort((a, b) => b.name.length - a.name.length);
  if (!valid.length) return esc(text);
  const names = [...new Set(valid.map(e => e.name))];
  const pattern = new RegExp(names.map(n => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "g");
  let result = "", pos = 0;
  for (const match of String(text).matchAll(pattern)) {
    result += esc(String(text).slice(pos, match.index));
    result += external(valid.find(e => e.name === match[0]).url, match[0], className);
    pos = match.index + match[0].length;
  }
  return result + esc(String(text).slice(pos));
}
function marks(values) {
  const cls = {[W.disputed]:"is-split"};
  return values.map(v => `<span class="mark ${cls[v] || ""}">${esc(v)}</span>`).join("");
}
function tags(values) {
  const cls = {[W.ai]:"ai", [W.social]:"social", [W.tag_pending]:"pend", [W.neither]:"none"};
  return values.map(v => `<span class="tag ${cls[v] || ""}">${esc(v)}</span>`).join("");
}
function leadText(event) {
  return event.leads.length ? event.leads.join("、") + (event.leads[0] === "Y Combinator" && event.leads.length === 1 ? "" : event.leads.length > 1 ? ` ${W.co_lead}` : ` ${W.lead}`) : W.lead_missing;
}
const companyRoute = id => `#c/${encodeURIComponent(id)}`;
const detailIcons = ["product", "problem", "business", "team", "funding", "competitor", "basis", "risk"];
const section = (name, content, number = null, id = "") => `<section class="sec section"${id ? ` id="${esc(id)}" tabindex="-1"` : ""}><h2 class="sec-t">${number === null ? "" : `<span class="no">${String(number).padStart(2, "0")}</span>${icon(detailIcons[number - 1] || "doc")}`}${esc(name)}</h2>${content}</section>`;
const empty = text => `<p class="empty-line">${esc(text)}</p>`;
function beijingDay(value) {
  const text = String(value || "");
  if (!/^\d{4}-\d{2}-\d{2}/.test(text)) return "";
  if (/[T ]\d{2}:\d{2}.*(?:Z|[+-]\d{2}:?\d{2})$/.test(text)) {
    const stamp = Date.parse(text.replace(" ", "T"));
    if (!Number.isNaN(stamp)) return new Date(stamp + 8 * 60 * 60 * 1000).toISOString().slice(0, 10);
  }
  return text.slice(0, 10);
}
function formatDate(value, today, weekday = true) {
  const day = beijingDay(value);
  if (!day) return String(value || "");
  const label = day.slice(0, 4) === String(today).slice(0, 4) ? day.slice(5) : day;
  return label + (weekday ? ` 周${"日一二三四五六"[new Date(day + "T00:00:00Z").getUTCDay()]}` : "");
}
function companyMark(event, large = false) {
  const key = String(event.company_id || event.id || event.name);
  const variant = [...key].reduce((n, ch) => n + ch.codePointAt(0), 0) % 6 + 1;
  return `<span class="cmark v${variant}${large ? " lg" : ""}" aria-hidden="true">${esc([...String(event.name).trim()][0] || "")}</span>`;
}
function splitSummary(summary) {
  const text = String(summary || "").trim();
  const stop = /[。.!?！？]/g;
  for (const match of text.matchAll(stop)) {
    const at = match.index;
    if (match[0] === "." && /\d/.test(text[at - 1] || "") && /\d/.test(text[at + 1] || "")) continue;
    let end = at + 1;
    while (end < text.length && /["'”’」』）)]/.test(text[end])) end++;
    return [text.slice(0, end), text.slice(end).trim()];
  }
  return [text, ""];
}
function amountClass(amount) {
  const text = String(amount || W.amount_missing);
  if (/待确认|说法不一|暂未换算/.test(text)) return " is-pend";
  return /未披露/.test(text) ? " is-none" : "";
}
function card(event, showBackfill = false, today = "") {
  const [product, description] = splitSummary(event.summary);
  const unsent = event.supplement_unsent;
  const state = unsent ? W.supplement_unsent : event.status;
  const reason = unsent ? "" : event.pending_reason;
  const next = event.next_push_label || "";
  return `<article class="co-row co-card has-mark" data-nav-item>${companyMark(event)}<div class="co-l">
    <div class="co-r1"><h3 class="co-name"><a class="co-link" href="${companyRoute(event.company_id)}">${esc(event.name)}</a>${marks(event.marks)}</h3><span class="co-country${!event.country || event.country === W.country_pending ? " is-pend" : ""}">${esc(event.country || W.country_pending)}</span><span class="co-tags">${tags(event.tags)}</span>${showBackfill && event.backfill && !unsent ? `<span class="mark is-supp">${esc(W.supplement)}</span>` : ""}${state !== W.admitted ? `<span class="state-flag">${esc(state)}${reason ? ` · ${esc(reason)}` : ""}</span>` : ""}${next && !unsent ? `<span class="push-flag next">${esc(next)}</span>` : ""}</div>
    ${product ? `<p class="co-prod"><span class="co-lbl">${esc(W.product_label)}：</span>${esc(product)}</p>` : ""}
    <p class="co-fund"><span>${esc(event.round)}</span><span class="co-amt-i${amountClass(event.amount)}">${esc(event.amount || W.amount_missing)}</span><span>${esc(leadText(event))}</span></p>
    ${description ? `<p class="co-desc"><span class="co-lbl">${esc(W.description)}：</span>${esc(description)}</p>` : ""}
    <p class="co-srcs"><span class="co-lbl">${esc(W.source_label)}</span>${event.sources.map((s, i) => `<span class="co-src-i">${external(s.url, s.name + (s.published_at ? ` · ${formatDate(s.published_at, today, false)}` : ""), "co-src", true)}${i < event.sources.length - 1 ? "、" : ""}</span>`).join("") || esc(W.missing)}</p>
    <p class="co-more-l"><a class="co-more" href="${companyRoute(event.company_id)}" aria-label="${esc(event.name)} ${esc(W.detail)}">${esc(W.detail)}${icon("right")}</a></p></div></article>`;
}
function dateCards(data, events, showBackfill = false) {
  const companies = new Map();
  for (const event of [...events].sort((a, b) => (b.push_date || "9999").localeCompare(a.push_date || "9999") ||
      (b.first_ingested_at || "").localeCompare(a.first_ingested_at || "") || b.id - a.id)) {
    const key = showBackfill ? `${event.company_id}:${event.issue_date || event.push_date || "pending"}` : event.company_id;
    if (!companies.has(key)) companies.set(key, event);
  }
  const rank = new Map(data.listing.map((id, i) => [id, i]));
  const selected = [...companies.values()].sort((a, b) => {
    const dayA = beijingDay(a.sources[0]?.published_at);
    const dayB = beijingDay(b.sources[0]?.published_at);
    return dayB.localeCompare(dayA) || (rank.get(a.id) ?? Infinity) - (rank.get(b.id) ?? Infinity);
  });
  let current, html = "";
  for (const event of selected) {
    const day = beijingDay(event.sources[0]?.published_at);
    if (day !== current) {
      if (current !== undefined) html += "</div></section>";
      current = day;
      const label = day ? formatDate(day, data.meta.today) : W.publication_unknown;
      html += `<section class="dgroup" data-date="${esc(day)}"><h2 class="dgroup-h"><span class="tnum">${esc(label)}</span></h2><div class="co-list">`;
    }
    html += card(event, showBackfill, data.meta.today);
  }
  return html + (selected.length ? "</div></section>" : "");
}
function fold(title, content) {
  return `<details class="fold"><summary><span>${esc(title)}</span><span class="act" aria-hidden="true"><span class="when-closed" data-label="展开"></span><span class="when-open" data-label="收起"></span>${icon("down")}</span></summary><div class="fold-b">${content}</div></details>`;
}
function weekHtml(data) {
  useWording(data);
  const week = data.week, byId = new Map(data.events.map(e => [e.id, e]));
  if (!week.date) return empty(W.no_new);
  const admitted = week.cards.map(id => byId.get(id));
  let html = admitted.length ? dateCards(data, admitted) : empty(W.no_new);
  let tail = "";
  for (const [key, title] of [["round_pending", W.round_pending]]) {
    if (week[key].length) tail += fold(title, week[key].map(id => {
      const e = byId.get(id);
      return `<div class="filtered-row"><a href="${companyRoute(e.company_id)}">${esc(e.name)}</a><span>${esc(e.pending_reason)}</span></div>`;
    }).join(""));
  }
  if (week.filtered.length) tail += fold(W.excluded, week.filtered.map(id => {
    const e = byId.get(id);
    return `<div class="filtered-row">${esc(e.name)}<span>${esc(e.filter_reasons[0])}</span></div>`;
  }).join(""));
  return html + (tail ? `<div class="week-tail">${tail}</div>` : "");
}
function ycHtml(data) {
  useWording(data);
  const ids = new Set(data.week.yc || []);
  const byId = new Map(data.events.map(e => [e.id, e]));
  const events = data.listing.filter(id => ids.has(id)).map(id => byId.get(id));
  return events.length ? `<div class="co-list">${events.map(e => card(e, false, data.meta.today)).join("")}</div>` : empty(W.no_matches);
}

function filterRow(filters, key, label, options) {
  return `<div class="filter-line"><span class="lab">${esc(label)}</span><div class="${key === "period" ? "seg" : "chips"}" role="group" aria-label="${esc(label)}">${options.map(([value, title, disabled]) => {
    const selected = filters[key] === value || key === "period" && value === "issue" && filters.period.startsWith("date:");
    return `<button type="button" class="${key === "period" ? "segment" : "chip"}${selected ? " on" : ""}" aria-pressed="${selected}" data-filter="${key}" data-value="${esc(value)}"${disabled ? " disabled" : ""}>${esc(title)}</button>`;
  }).join("")}</div></div>`;
}
function listHtml(data, filters) {
  useWording(data);
  const active = activeFilterCount(filters);
  const badge = active ? `<span class="n tnum">${active}</span>` : "";
  const quickReset = active ? `<button type="button" class="btn text" data-clear-filters>重置筛选</button>` : "";
  const inlineReset = active ? `<button type="button" class="clear-f" data-clear-filters>重置筛选</button>` : "";
  let html = `<div class="tb"><button class="btn fbtn" type="button" id="open-filters" aria-controls="filter-panel" aria-expanded="false">${icon("filter")}筛选${badge}</button>${quickReset}</div><div id="filter-scrim" class="scrim" hidden></div><section class="fpanel drawer" id="filter-panel" aria-labelledby="filter-title"><div class="drawer-grip"></div><div class="drawer-h"><h2 id="filter-title">筛选</h2><button type="button" class="btn text" data-close-filters aria-label="关闭筛选">${icon("close")}</button></div><div class="filters">${filterRow(filters, "period", W.filter_period, [["week", W.week], ["issue", W.select_date, !data.digests.length], ["30", W.recent], ["all", W.all]])}`;
  if (filters.period.startsWith("date:")) html += `<div class="filter-line"><label for="issue" class="lab">${esc(W.issue)}</label><span class="select"><select id="issue">${data.digests.map(d => `<option value="date:${d.date}"${filters.period === `date:${d.date}` ? " selected" : ""}>${esc(formatDate(d.date, data.meta.today))}那期</option>`).join("")}</select>${icon("down")}</span></div>`;
  html += filterRow(filters, "tag", W.tag, [W.all, W.ai, W.social, W.neither, W.tag_pending].map((v, i) => [i ? v : "all", v]));
  html += filterRow(filters, "round", W.round, [W.all, W.angel, W.seed, W.series_a].map((v, i) => [i ? v : "all", v]));
  html += filterRow(filters, "status", W.status, [W.all, W.admitted, W.round_pending].map((v, i) => [i ? v : "all", v]));
  html += `<div class="filter-line"><span class="lab"></span><div class="chips"><button type="button" class="chip${filters.backfill ? " on" : ""}" aria-pressed="${!!filters.backfill}" data-backfill-mark>${esc(W.supplement)}</button>${inlineReset}</div></div></div><div class="drawer-f"><button type="button" class="btn" data-reset-filters>重置</button><button type="button" class="btn primary" data-close-filters>完成</button></div></section><div id="list-results">`;
  const events = selectEvents(data, filters);
  if (filters.unsentOnly) html += `<div class="notice">${esc(W.supplement_unsent)} <a href="#list" data-clear-backfill>${esc(W.view_all)}</a></div>`;
  html += dateCards(data, events, true);
  return html + (events.length ? "" : empty(W.no_matches)) + "</div>";
}
function pageAddress(url) {
  try {
    const parsed = new URL(url);
    return {host:parsed.hostname.toLowerCase().replace(/^www\./, ""), path:parsed.pathname.replace(/\/+$/, ""), query:parsed.search};
  } catch (_) { return {host:"", path:"", query:""}; }
}
function samePage(url) {
  const {host, path, query} = pageAddress(url);
  return host ? host + path + query : String(url || "");
}
function sourceLabel(s, companyNames = []) {
  const host = pageAddress(s.url).host;
  const name = String(s.name || "").trim().replace(/^www\./i, "");
  if (!name || name === "来源" || name.toLowerCase() === host) return host || name || s.url;
  const title = String(s.title || "").trim();
  const same = value => String(value || "").trim().toLowerCase() === title.toLowerCase();
  return title && !same(name) && !companyNames.some(same) ? `${name} · ${title}` : name;
}
function sourceLabels(sources, companyNames = []) {
  const pages = [], seen = new Set();
  for (const source of sources) {
    const key = samePage(source.url);
    if (seen.has(key)) continue;
    seen.add(key);
    pages.push({key, source, base:sourceLabel(source, companyNames)});
  }
  const groups = new Map(), bare = new Set();
  pages.forEach((page, i) => groups.set(page.base, [...(groups.get(page.base) || []), i]));
  for (const members of groups.values()) bare.add(members.find(i => !pageAddress(pages[i].source.url).path) ?? members[0]);
  const labels = new Map(), used = new Set(pages.filter((_, i) => bare.has(i)).map(page => page.base));
  pages.forEach(({key, source, base}, i) => {
    let label = base;
    if (!bare.has(i)) {
      const {host, path, query} = pageAddress(source.url);
      const prefix = base === host ? "" : `${base} · `;
      const choices = base === host ? [host + path, host + path + query] : [host, host + path, host + path + query];
      label = choices.map(part => prefix + part).find(candidate => !used.has(candidate));
      if (!label) {
        label = prefix + host + path + query;
        while (used.has(label)) label += ` · ${source.url}`;
      }
    }
    labels.set(key, label);
    used.add(label);
  });
  return labels;
}
function shortStamp(stamp, today) {
  const text = String(stamp || "");
  const day = beijingDay(text);
  let time = text.slice(11, 16);
  if (/[T ]\d{2}:\d{2}.*(?:Z|[+-]\d{2}:?\d{2})$/.test(text)) {
    const parsed = Date.parse(text.replace(" ", "T"));
    if (!Number.isNaN(parsed)) time = new Date(parsed + 8 * 60 * 60 * 1000).toISOString().slice(11, 16);
  }
  return day ? `${formatDate(day, today, false)}${time ? ` ${time}` : ""}` : "";
}
function sourceTime(s, today) {
  const et = s.original_date_et ? `${W.original_date}${formatDate(s.original_date_et, today, false)}` : "";
  if (s.published_at) return `${W.beijing} ${shortStamp(s.published_at, today)}${et ? ` · ${et}` : ""}`;
  if (et) return et;
  return s.beijing_at ? `${W.page_no_date} · ${formatDate(s.beijing_at, today, false)} ${W.read_on}` : W.page_no_date;
}
function sourceList(sources, today = "", labels = sourceLabels(sources), companyNames = []) {
  const pages = new Map();
  for (const source of sources) if (!pages.has(samePage(source.url))) pages.set(samePage(source.url), source);
  return [...pages.values()].map(s => `<div class="source-row">${external(s.url, labels.get(samePage(s.url)) || sourceLabel(s, companyNames))}<div class="muted">${esc(sourceTime(s, today))}</div></div>`).join("") || W.missing;
}
function evidenceHtml(evidence, today = "", labels = new Map(), companyNames = []) {
  return evidence.map(e => `<blockquote>${esc(e.quote)}</blockquote><div class="source">${esc(W.source_label)}${external(e.url, labels.get(samePage(e.url)) || sourceLabel(e, companyNames))}<div class="muted">${esc(sourceTime(e, today))}</div></div>`).join("");
}
function newEvidenceHtml(evidence, today, labels, companyNames) {
  return evidence.map((entry, i) => `<section class="evidence" aria-labelledby="h-new-evidence-${i}"><div class="ev-h" id="h-new-evidence-${i}">${icon("info")}<b>${esc(W.evidence)}${esc(entry.field)}</b></div><p class="ev-vals">${esc(W.pushed_value)}${esc(entry.pushed)}；${esc(W.found_value)}${esc(entry.found)}</p><ul>${entry.evidence.map(e => `<li><span class="ev-src">${external(e.url, labels.get(samePage(e.url)) || sourceLabel(e, companyNames))}</span><q>${esc(e.quote)}</q><span class="muted">${esc(sourceTime(e, today))}</span></li>`).join("")}</ul></section>`).join("");
}
function fundingHistoryHtml(data, company, entities, today) {
  const events = company.history.map(id => data.events.find(e => e.id === id)).sort((a, b) =>
    beijingDay(b.date).localeCompare(beijingDay(a.date)) || b.id - a.id);
  return `<section class="sec" id="fund-hist" tabindex="-1" aria-labelledby="h-fund-hist"><h2 class="sec-t" id="h-fund-hist">${icon("funding")}${esc(W.history)}</h2><ol class="ftl">${events.map(e => {
    const amount = String(e.amount || "").trim();
    const noAmount = !amount || [W.amount_missing, W.undisclosed].includes(amount);
    const noRound = !e.round || [W.round_missing, W.round_pending, W.undisclosed, W.unknown].includes(e.round);
    const when = e.yc_batch ? `${e.yc_batch}（YC）` : e.date ? formatDate(e.date, today, false) : "以前";
    return `<li class="ftl-item${e.id === company.event_id ? " is-current" : ""}${noRound ? " is-unknown" : ""}" data-event-id="${esc(e.id)}"><span class="ftl-when tnum">${esc(when)}</span><span class="ftl-dot" aria-hidden="true"></span><div class="ftl-body"><div class="ftl-what"><span class="ftl-round">${esc(noRound ? W.round_missing : roundText(e))}</span><span class="ftl-amt tnum${noAmount ? " is-none" : ""}">${esc(noAmount ? W.amount_missing : amount)}</span></div><p class="ftl-who">${linkedText(leadText(e), entities, "ext-link")}${e.follows?.length ? `；${linkedText(e.follows.join("、"), entities, "ext-link")} ${esc(W.follow)}` : ""}</p></div></li>`;
  }).join("")}</ol></section>`;
}
function conflictsHtml(conflicts) {
  return conflicts.length ? section(W.conflicts, conflicts.map(c => `<div class="diffnote">${icon("warn")}<div><b>${esc(c.field)}</b><ul>${c.values.map(v => `<li><span class="muted">${external(v.url, v.name)}</span>：${esc(v.value)}</li>`).join("")}</ul></div></div>`).join("")) : "";
}
function fundingHtml(event, today) {
  const values = [[W.current_round, `${roundText(event)} · ${event.amount || W.amount_missing}`],
    [W.lead, event.leads.length ? event.leads.join("、") : W.missing],
    [W.follow, event.follows.length ? event.follows.join("、") : W.missing],
    [W.date, event.date ? formatDate(event.date, today, false) : W.missing]];
  return `<dl class="kv">${values.map(([key, value]) => `<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join("")}</dl>`;
}
function deepHtml(deep, event, today = "", labels = new Map(), companyNames = []) {
  if (deep.status === W.skipped) return "";
  if (deep.status !== "完成") {
    const message = deep.status === W.absent ? W.deep_absent : deep.status === W.failed ? W.deep_failed : W.deep_pending;
    if (message === W.deep_pending) return section(W.deep, `<div class="sec-pending"><p class="pending-note" role="status">${icon("loading")}${esc(message)}</p><div class="deep-skeleton" aria-hidden="true">${Array.from({length: 8}, () => '<div class="skl-section"><div class="skl skl-title"></div><div class="skl skl-line"></div><div class="skl skl-line"></div></div>').join("")}</div></div>`);
    return section(W.deep, `<div class="sec-nodeep"><p class="nodeep-note" role="status">${icon("info")}${esc(message)}</p></div>`);
  }
  return deep.items.map((item, i) => section(item.name, i === 4 ? fundingHtml(event, today) : (item.points.length ? item.points.map(p => `<div class="point"><div class="point-label">${esc(W.point)}</div><p>${linkedText(p.text, [...deep.entities, ...p.entities])}</p>${p.evidence.length ? `<div class="point-label">${esc(W.quote)}</div>${evidenceHtml(p.evidence, today, labels, companyNames)}` : `<p class="muted">${esc(W.missing)}</p>`}</div>`).join("") : `<p>${esc(W.missing)}</p>`) + item.missing.map(m => `<p>${esc(m)}：${esc(W.missing)}</p>`).join(""), i + 1, `deep-${i + 1}`)).join("");
}
function roundText(event, original = false) {
  const round = event.round || W.round_pending, text = String(event.round_text || "").trim();
  return original && !event.out_of_scope && !round.startsWith(W.out_of_scope) && text &&
    !/[\u3400-\u9fff]/.test(text) && text.toLowerCase() !== round.trim().toLowerCase() ? `${round}（原文 ${text}）` : round;
}
function keyFieldsHtml(event, entities, baseline = false, verification = [], website = "", today = "") {
  const amount = String(event.amount || "").trim();
  const hasAmount = amount && !/未披露/.test(amount) && ![W.missing, W.unknown].includes(amount);
  const round = String(event.round || "").trim();
  const hasRound = round && ![W.round_pending, W.round_missing, W.undisclosed, W.missing, W.unknown].includes(round);
  const lines = [];
  if (hasAmount) lines.push(linkedText(amount, entities));
  if (hasRound) lines.push(esc(roundText(event, true)));
  if (event.leads.length) lines.push(`${esc(W.lead)}：${linkedText(event.leads.length === 1 && event.leads[0] === "Y Combinator" ? leadText(event) : event.leads.join("、"), entities)}`);
  if (event.follows.length) lines.push(`${esc(W.follow)}：${linkedText(event.follows.join("、"), entities)}`);
  const funding = lines.map((line, i) => i ? `<small>${line}</small>` : line).join("");
  const location = [event.country, event.city, event.country_basis].filter(v => v && v !== W.city_pending && v !== W.missing).join(" · ");
  const values = funding ? [[W.current_round, funding]] : [];
  if (event.country) values.push([W.country_basis, location]);
  if (website) values.push([W.homepage, website]);
  for (const [key, value] of [[W.date, event.date], [W.first_seen, event.first_seen], [W.push_date, event.push_date]]) {
    if (key === W.push_date && (baseline || event.baseline_unpushed || event.out_of_scope || event.filter_reasons?.length || event.round?.startsWith(W.out_of_scope))) continue;
    if (value) values.push([key, formatDate(value, today, false)]);
  }
  const notes = verification.map(b => {
    const field = [W.amount, W.round, W.original_round, "币种"].includes(b.field) ? W.amount : b.field;
    return `<p class="facts-note">${b.text === W.verified ? "" : "<b>待确认：</b>"}${field ? `${esc(field)} · ` : ""}${esc(b.text)}${b.reason ? ` · ${esc(b.reason)}` : ""}${b.urls.map((url, i) => ` ${external(url, b.quotes[i] || W.source_label)}`).join("")}</p>`;
  }).join("");
  return section(W.key_fields, `<div class="card fact-card"><dl class="facts">${values.map(([k, v]) =>
    `<div${k === W.current_round && hasAmount ? ' class="amt"' : ""}><dt>${esc(k)}</dt><dd>${k === W.current_round ? v : k === W.homepage ? external(v, W.homepage, "ext-link", true) : linkedText(v, entities)}</dd></div>`
  ).join("")}</dl>${notes}</div>`);
}
function verifySourcesHtml(company) {
  return company.verification_sources?.length ? section(W.verify_sources, company.verification_sources.map(s => `<p>${external(s.url, s.quote || W.source_label)}</p>`).join("")) : "";
}
function detailNav(data, id) {
  const ids = new Set([...(data.week?.cards || []), ...(data.week?.round_pending || [])]);
  return data.events.some(e => e.company_id === id && ids.has(e.id)) ? "week" : "list";
}
function companyHeadHtml(company, nav = "list") {
  const country = company.header.country || W.country_pending;
  return `<nav class="crumb" aria-label="位置"><a href="#${nav}">${icon("left")}${esc(nav === "week" ? W.week : W.archive)}</a><span class="sep" aria-hidden="true">›</span><span aria-current="page">${esc(company.header.name)}</span></nav><header class="co-head"><div class="dhead">${companyMark({company_id:company.id, name:company.header.name}, true)}<h1>${company.website ? external(company.website, company.header.name) : esc(company.header.name)}</h1><div class="co-head-sub">${marks(company.header.marks || [])}${tags(company.header.tags || [])}${company.backfill ? `<span class="mark is-supp">${esc(W.supplement)}</span>` : ""}</div></div><p class="head-country${country === W.country_pending ? " is-pend" : ""}">${esc(country)}</p></header>`;
}

function detailHtml(data, id) {
  useWording(data);
  const today = data.meta?.today || "";
  const company = data.companies.find(c => c.id === id);
  if (!company) return empty(W.no_company);
  const event = data.events.find(e => e.id === company.event_id), deep = company.deepdive;
  const nav = detailNav(data, id);
  const companyNames = [company.header.name, ...data.events.filter(e => e.company_id === id).map(e => e.name)];
  const labels = sourceLabels(company.sources, companyNames);
  if (company.baseline_unpushed) {
    return companyHeadHtml(company, nav) +
      keyFieldsHtml(event, [], true, company.verification, company.website, today) + section(W.all_sources, sourceList(company.sources, today, labels, companyNames)) + verifySourcesHtml(company);
  }
  const entities = [{name:company.header.name, url:company.website}, ...deep.entities, ...deep.items.flatMap(i => i.points.flatMap(p => p.entities))];
  let html = companyHeadHtml(company, nav);
  if (company.identity_note) html += `<div class="alert">${esc(company.identity_note)}</div>`;
  html += newEvidenceHtml(deep.new_evidence, today, labels, companyNames);
  html += section(W.description, `<p>${linkedText(company.summary || W.missing, entities)}</p>`);
  const sources = section(W.all_sources, sourceList(company.sources, today, labels, companyNames), null, "all-sources") + verifySourcesHtml(company);
  if (deep.status !== W.skipped) {
    html += keyFieldsHtml(event, entities, false, company.verification, company.website, today);
    html += `<div class="detail v2"><nav class="toc" aria-label="${esc(W.deep)}"${deep.status !== "完成" ? " hidden" : ""}>${deep.items.map((item, i) => `<a href="${companyRoute(id)}" data-detail-section="deep-${i + 1}"${i === 0 ? ' aria-current="true"' : ""}>${esc(item.name)}</a>`).join("")}<a href="${companyRoute(id)}" data-detail-section="fund-hist">${esc(W.history)}</a><a href="${companyRoute(id)}" data-detail-section="all-sources">${esc(W.all_sources)}</a></nav><div class="detail-content">`;
    html += deepHtml(deep, event, today, labels, companyNames);
    html += fundingHistoryHtml(data, company, entities, today);
    html += conflictsHtml(company.conflicts);
    return html + sources + "</div></div>";
  }
  return html + sources;
}
function createDetailLoader(data, fetcher = url => fetch(url)) {
  const requests = new Map();
  return function loadDetail(id) {
    const company = data.companies.find(c => c.id === id);
    if (!company) return Promise.resolve(null);
    if (!company.detail_url) return Promise.resolve(data);
    if (!requests.has(id)) {
      const request = Promise.resolve().then(() => fetcher(company.detail_url)).then(response => {
        if (!response.ok) throw new Error("详情读取失败");
        return response.json();
      }).then(detail => {
        if (!Array.isArray(detail.events) || !detail.companies?.some(c => c.id === id &&
            (c.baseline_unpushed || c.deepdive && Array.isArray(c.history))))
          throw new Error("详情内容无效");
        return {...detail, meta:data.meta, week:data.week};
      }).catch(error => { requests.delete(id); throw error; });
      requests.set(id, request);
    }
    return requests.get(id);
  };
}

if (typeof module !== "undefined") module.exports = {selectEvents, filtersForRoute, defaultFilters, activeFilterCount, external, linkedText, weekHtml, listHtml, detailHtml, ycHtml, createDetailLoader, splitSummary, formatDate, sourceLabels, samePage};
if (typeof document !== "undefined") {
  let data, loadDetail, renderVersion = 0, lastHash, drawerOpen = false, detailObserver;
  const filters = defaultFilters();
  function setTheme(value) {
    const root = document.documentElement;
    if (!root) return;
    if (value === "light" || value === "dark") root.setAttribute("data-theme", value); else root.removeAttribute("data-theme");
    try { localStorage.setItem("vcr-theme", value); } catch (_) {}
    document.querySelectorAll("[data-theme-set]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.themeSet === value)));
  }
  function cycleTheme() {
    const current = document.documentElement?.getAttribute("data-theme") || (typeof matchMedia === "function" && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    setTheme(current === "dark" ? "light" : "dark");
  }
  let savedTheme = "system";
  try { savedTheme = localStorage.getItem("vcr-theme") || "system"; } catch (_) {}
  setTheme(savedTheme);
  function setDrawer(open, focusedFilter = null) {
    const panel = document.getElementById("filter-panel"), trigger = document.getElementById("open-filters");
    if (!panel) return;
    drawerOpen = open;
    panel.classList.toggle("is-open", open);
    document.getElementById("filter-scrim").hidden = !open;
    document.getElementById("filter-scrim").classList.toggle("is-open", open);
    trigger.setAttribute("aria-expanded", String(open));
    if (open) { panel.setAttribute("role", "dialog"); panel.setAttribute("aria-modal", "true"); }
    else { panel.removeAttribute("role"); panel.removeAttribute("aria-modal"); }
    document.body.style.overflow = open ? "hidden" : "";
    document.querySelectorAll(".nav-side, .tabbar, .appbar, #list-results").forEach(el => { el.inert = open; });
    if (open) {
      const selected = focusedFilter && [...panel.querySelectorAll("[data-filter]")].find(b => b.dataset.filter === focusedFilter.key && b.dataset.value === focusedFilter.value);
      (selected || panel.querySelector("button"))?.focus();
    } else if (focusedFilter) trigger.focus();
  }
  async function render() {
    if (!data) return;
    const version = ++renderVersion;
    const hash = location.hash || "#week", isDetail = hash.startsWith("#c/");
    filtersForRoute(hash, lastHash, filters);
    lastHash = hash;
    let id;
    if (isDetail) {
      try { id = decodeURIComponent(hash.slice(3)); } catch (_) { id = ""; }
    }
    const route = isDetail ? detailNav(data, id) : hash === "#backfill" ? "list" : hash === "#yc" ? "yc" : ["#week", "#list"].includes(hash) ? hash.slice(1) : "week";
    if (isDetail || route !== "list") {
      drawerOpen = false;
      if (document.body) document.body.style.overflow = "";
      document.querySelectorAll(".nav-side, .tabbar, .appbar").forEach(el => { el.inert = false; });
    }
    document.querySelectorAll("[data-nav]").forEach(a => {
      a.classList.toggle("on", a.dataset.nav === (route === "yc" ? "week" : route));
      if (a.dataset.nav === (route === "yc" ? "week" : route)) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    let title = {week:W.product, list:W.archive, yc:W.yc}[route];
    if (isDetail) {
      title = data.companies.find(c => c.id === id)?.header?.name || data.events.find(e => e.company_id === id)?.name || W.company_detail;
    }
    document.title = `${title} · ${W.product}`;
    document.getElementById("main").classList?.toggle("list", !isDetail);
    let html;
    if (isDetail) {
      document.getElementById("main").innerHTML = empty(W.loading_detail);
      try {
        const detail = await loadDetail(id);
        html = detail ? detailHtml(detail, id) : empty(W.no_company);
      } catch (_) {
        html = empty(W.detail_failed) + `<button type="button" class="btn" id="retry-detail">${esc(W.retry)}</button>`;
      }
      if (version !== renderVersion) return;
    } else html = route === "list" ? listHtml(data, filters) : route === "yc" ? ycHtml(data) : weekHtml(data);
    document.getElementById("main").innerHTML = html;
    if (detailObserver) detailObserver.disconnect();
    if (isDetail && typeof IntersectionObserver !== "undefined") {
      const links = [...document.querySelectorAll("[data-detail-section]")];
      detailObserver = new IntersectionObserver(entries => {
        const entry = entries.find(item => item.isIntersecting);
        if (entry) links.forEach(link => link.setAttribute("aria-current", String(link.dataset.detailSection === entry.target.id)));
      }, {rootMargin:"-10% 0px -70% 0px"});
      links.forEach(link => { const target = document.getElementById(link.dataset.detailSection); if (target) detailObserver.observe(target); });
    }
    if (route === "list" && drawerOpen) setDrawer(true);
  }
  document.addEventListener("click", e => {
    if (e.target.closest("[data-skip-main]")) { e.preventDefault(); document.getElementById("main").focus(); return; }
    const sectionLink = e.target.closest("[data-detail-section]");
    if (sectionLink) {
      e.preventDefault();
      const target = document.getElementById(sectionLink.dataset.detailSection);
      if (target) { target.focus({preventScroll:true}); target.scrollIntoView({block:"start"}); }
      return;
    }
    const theme = e.target.closest("[data-theme-set]");
    if (theme) { setTheme(theme.dataset.themeSet); return; }
    if (e.target.closest("[data-theme-cycle]")) { cycleTheme(); return; }
    if (e.target.closest("#open-filters")) { setDrawer(true); return; }
    if (e.target.closest("[data-close-filters], #filter-scrim")) { setDrawer(false, {}); return; }
    if (e.target.closest("[data-reset-filters], [data-clear-filters]")) { Object.assign(filters, defaultFilters()); render(); return; }
    if (e.target.closest("#retry-detail")) { render(); return; }
    if (e.target.closest("[data-clear-backfill]")) { Object.assign(filters, defaultFilters()); render(); return; }
    if (e.target.closest("[data-backfill-mark]")) { filters.backfill = !filters.backfill; render(); return; }
    const button = e.target.closest("[data-filter]");
    if (button) {
      filters[button.dataset.filter] = button.dataset.value === "issue" ? `date:${data.digests[0].date}` : button.dataset.value;
      render().then(() => { if (drawerOpen) setDrawer(true, {key:button.dataset.filter, value:button.dataset.value}); });
    }
  });
  document.addEventListener("change", e => {
    if (e.target.id === "issue" && e.target.value) { filters.period = e.target.value; render(); }
  });
  document.addEventListener("keydown", e => {
    if (drawerOpen) {
      if (e.key === "Escape") { e.preventDefault(); setDrawer(false, {}); return; }
      if (e.key === "Tab") {
        const focusable = [...document.getElementById("filter-panel").querySelectorAll("button:not([disabled]), select, a[href]")];
        const first = focusable[0], last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
      return;
    }
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const tagName = document.activeElement?.tagName;
    if (tagName === "INPUT" || tagName === "SELECT" || tagName === "TEXTAREA") return;
    if (e.key === "t") { e.preventDefault(); cycleTheme(); return; }
    if (e.key === "j" || e.key === "k") {
      const links = [...document.querySelectorAll(".co-card .co-link")];
      if (!links.length) return;
      e.preventDefault();
      const idx = links.indexOf(document.activeElement);
      const next = e.key === "j" ? (idx < 0 ? 0 : Math.min(idx + 1, links.length - 1)) : (idx <= 0 ? 0 : idx - 1);
      links[next]?.focus();
      links[next]?.scrollIntoView?.({block:"nearest"});
    }
  });
  window.addEventListener("resize", () => { if (drawerOpen && matchMedia("(min-width: 1024px)").matches) setDrawer(false); });
  window.addEventListener("hashchange", () => { render(); window.scrollTo(0, 0); });
  fetch("./data.json").then(r => { if (!r.ok) throw new Error(); return r.json(); }).then(value => {
    data = value; useWording(data); loadDetail = createDetailLoader(data); render();
  }).catch(() => {
    const main = document.getElementById("main");
    main.innerHTML = empty(W.load_failed || main.dataset?.loadFailed);
  });
}
