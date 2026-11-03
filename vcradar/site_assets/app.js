"use strict";
const W = {};
const useWording = data => Object.assign(W, data.wording || {});
const phrase = (key, values) => W[key].replace(/\{(\w+)\}/g, (_, k) => String(values[k]));
const defaultFilters = () => ({period:"all", tag:"all", round:"all", status:"all", backfill:false, unsentOnly:false});
function filtersForRoute(hash, previous, filters) {
  if (hash !== previous && hash === "#backfill") Object.assign(filters, defaultFilters(), {unsentOnly:true});
  if (hash !== previous && hash !== "#backfill") filters.unsentOnly = false;
  return filters;
}

function selectEvents(data, filters) {
  const allowed = new Set(data.listing);
  return data.events.filter(e => {
    if (!allowed.has(e.id) || e.filter_reasons?.length) return false;
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
function external(url, label) {
  try {
    const parsed = new URL(url);
    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) return esc(label);
  } catch (_) { return esc(label); }
  return `<a href="${esc(url)}" target="_blank" rel="noopener nofollow">${esc(label)}</a>`;
}
function linkedText(text, entities) {
  const valid = entities.filter(e => e.name && e.url).sort((a, b) => b.name.length - a.name.length);
  if (!valid.length) return esc(text);
  const names = [...new Set(valid.map(e => e.name))];
  const pattern = new RegExp(names.map(n => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "g");
  let result = "", pos = 0;
  for (const match of String(text).matchAll(pattern)) {
    result += esc(String(text).slice(pos, match.index));
    result += external(valid.find(e => e.name === match[0]).url, match[0]);
    pos = match.index + match[0].length;
  }
  return result + esc(String(text).slice(pos));
}
function marks(values) {
  const cls = {[W.new_round]:"new", [W.large]:"big", [W.backfilled]:"back", [W.disputed]:"amount"};
  return values.map(v => `<span class="mark ${cls[v] || ""}">${esc(v)}</span>`).join("");
}
function tags(values) {
  const cls = {[W.ai]:"ai", [W.social]:"social", [W.tag_pending]:"pending"};
  return values.map(v => `<span class="tag ${cls[v] || ""}">${esc(v)}</span>`).join("");
}
function leadText(event) {
  return event.leads.length ? event.leads.join("、") + (event.leads[0] === "Y Combinator" && event.leads.length === 1 ? "" : event.leads.length > 1 ? ` ${W.co_lead}` : ` ${W.lead}`) : W.lead_missing;
}
const companyRoute = id => `#c/${encodeURIComponent(id)}`;
const section = (name, content) => `<section class="section"><h2>${esc(name)}</h2>${content}</section>`;
const empty = text => `<p class="empty">${esc(text)}</p>`;
function card(event, showBackfill = false) {
  return `<article class="card"><h2><a href="${companyRoute(event.company_id)}">${esc(event.name)}</a>${marks(event.marks)}${showBackfill && event.backfill ? `<span class="backfill">${esc(W.supplement)}</span>` : ""}｜${esc(event.country || W.country_pending)}</h2>
    <div class="round">${esc(event.round)} · ${esc(event.amount || W.amount_missing)} · ${esc(leadText(event))}</div>
    <div>${tags(event.tags)}${event.status !== W.admitted ? `<span class="badge">${esc(event.status)} · ${esc(event.pending_reason)}</span>` : ""}</div>
    <p>${esc(event.summary || W.missing)}</p>
    ${event.next_push_label ? `<div class="sub">${esc(event.next_push_label)}</div>` : ""}
    <div class="footer"><span>${esc(W.source_label)}${event.sources.map(s => external(s.url, `${s.name} · ${(s.beijing_at || "").slice(5, 10)}`)).join("　") || W.missing}</span><a href="${companyRoute(event.company_id)}">${esc(W.detail)}</a></div></article>`;
}
function weekHtml(data) {
  useWording(data);
  const week = data.week, byId = new Map(data.events.map(e => [e.id, e]));
  if (!week.date) return empty(W.no_push);
  const admitted = week.cards.map(id => byId.get(id));
  let text = phrase("counts", {period:week.period_label || W.week, count:new Set(admitted.map(e => e.company_id)).size,
    ai:admitted.filter(e => e.tags.includes(W.ai)).length, social:admitted.filter(e => e.tags.includes(W.social)).length,
    new_round:admitted.filter(e => e.marks.includes(W.new_round)).length, large:admitted.filter(e => e.marks.includes(W.large)).length});
  const extra = [];
  if (week.round_pending.length) extra.push(phrase("pending_count", {count:week.round_pending.length}));
  if (week.filtered.length) extra.push(phrase("filtered", {count:week.filtered.length}));
  if (extra.length) text += ` · ${extra.join(" · ")}`;
  let html = `<p class="sub">${esc(week.date)} ${esc(W.push)}</p><p class="summary">${esc(week.counts_line || text)}</p>`;
  const failure = W.week_failed[week.status_key];
  if (week.hints_line || failure) html += `<div class="alert">${esc(week.hints_line || failure)} <a href="#sources">${esc(W.sources)}</a></div>`;
  html += admitted.length ? admitted.map(e => card(e)).join("") : empty(failure ? W.no_available : W.no_new);
  for (const [key, title] of [["round_pending", W.round_pending]]) {
    if (week[key].length) html += section(title, week[key].map(id => {
      const e = byId.get(id);
      return `<div class="mini"><a href="${companyRoute(e.company_id)}">${esc(e.name)}</a> · <span class="why">${esc(e.pending_reason)}</span></div>`;
    }).join(""));
  }
  if (week.filtered.length) html += `<details class="section"><summary>${esc(phrase("filtered", {count:week.filtered.length}))}：${esc(Object.entries(week.filtered_counts).map(([reason, count]) => `${reason} ${count}`).join(" · "))}</summary>${week.filtered.map(id => {
    const e = byId.get(id);
    return `<div class="mini">${esc(e.name)} · ${esc(e.filter_reasons[0])}</div>`;
  }).join("")}</details>`;
  return html;
}
function ycHtml(data) {
  useWording(data);
  const ids = new Set(data.week.yc || []);
  const byId = new Map(data.events.map(e => [e.id, e]));
  const events = data.listing.filter(id => ids.has(id)).map(id => byId.get(id));
  return events.length ? events.map(e => card(e)).join("") : empty(W.no_matches);
}

function filterRow(filters, key, label, options) {
  return `<div class="filter-row"><span class="filter-label">${esc(label)}</span>${options.map(([value, title]) => `<button type="button" class="chip${filters[key] === value ? " on" : ""}" aria-pressed="${filters[key] === value}" data-filter="${key}" data-value="${esc(value)}">${esc(title)}</button>`).join("")}</div>`;
}
function listHtml(data, filters) {
  useWording(data);
  let html = `<div class="filters">${filterRow(filters, "period", W.filter_period, [["week", W.week], ["30", W.recent], ["all", W.all]])}`;
  html += `<div class="filter-row"><label for="issue" class="filter-label">${esc(W.issue)}</label><select id="issue"><option value="">${esc(W.select_date)}</option>${data.digests.map(d => `<option value="date:${d.date}"${filters.period === `date:${d.date}` ? " selected" : ""}>${esc(d.date)}</option>`).join("")}</select></div>`;
  html += filterRow(filters, "tag", W.tag, [W.all, W.ai, W.social, W.neither, W.tag_pending].map((v, i) => [i ? v : "all", v]));
  html += filterRow(filters, "round", W.round, [W.all, W.angel, W.seed, W.series_a, W.round_pending].map((v, i) => [i ? v : "all", v]));
  html += filterRow(filters, "status", W.status, [W.all, W.admitted, W.round_pending].map((v, i) => [i ? v : "all", v]));
  html += `<button type="button" class="chip${filters.backfill ? " on" : ""}" aria-pressed="${!!filters.backfill}" data-backfill-mark>${esc(W.supplement)}</button></div>`;
  const events = selectEvents(data, filters);
  if (filters.unsentOnly) html += `<div class="notice">${esc(phrase("unsent_count", {count:new Set(events.map(e => e.company_id)).size}))} <a href="#list" data-clear-backfill>${esc(W.view_all)}</a></div>`;
  html += `<p class="sub">${esc(phrase("count", {count:events.length}))}</p>`;
  let current;
  for (const event of events) {
    const group = event.push_date || (event.baseline_unpushed ? "baseline" : "pending");
    if (group !== current) {
      current = group;
      html += `<div class="day">${esc(event.push_date ? `${event.push_date} ${W.push}` : event.supplement_unsent ? W.supplement_unsent : event.baseline_unpushed ? W.baseline : event.next_push_label)}</div>`;
    }
    html += card(event, true);
  }
  return html + (events.length ? "" : empty(W.no_matches));
}
function sourceList(sources) {
  return sources.map(s => `<div class="source-row">${external(s.url, s.title || s.name || s.url)}<div class="muted">${esc(W.beijing)} ${esc(s.beijing_at || W.unknown)} · ${esc(W.original_date)}${esc(s.original_date_et || W.unknown)}</div></div>`).join("") || W.missing;
}
function evidenceHtml(evidence) {
  return evidence.map(e => `<blockquote>${esc(e.quote)}</blockquote><div class="source">${esc(W.source_label)}${external(e.url, e.name || e.url)} · ${esc(W.beijing)} ${esc(e.beijing_at || W.unknown)}</div>`).join("");
}
function deepHtml(deep) {
  if (deep.status === W.skipped) return "";
  if (deep.status !== "完成") {
    const message = deep.status === W.absent ? W.deep_absent : deep.status === W.failed ? W.deep_failed : W.deep_pending;
    return section(W.deep, `<p>${message}</p>`);
  }
  return deep.items.map(item => section(item.name, (item.points.length ? item.points.map(p => `<div class="point"><div class="point-label">${esc(W.point)}</div><p>${linkedText(p.text, [...deep.entities, ...p.entities])}</p>${p.evidence.length ? `<div class="point-label">${esc(W.quote)}</div>${evidenceHtml(p.evidence)}` : `<p class="muted">${esc(W.missing)}</p>`}</div>`).join("") : `<p>${esc(W.missing)}</p>`) + item.missing.map(m => `<p>${esc(m)}：${esc(W.missing)}</p>`).join(""))).join("");
}
function keyFieldsHtml(event, entities, baseline = false, verification = [], website = "") {
  const location = [event.country || W.country_pending, event.city || W.city_pending, event.country_basis || W.missing].join(" · ");
  const values = [[W.round, event.round], [W.original_round, event.round_text || W.undisclosed], [W.amount, event.amount || W.amount_missing], [W.lead, leadText(event)], [W.follow, event.follows.join("、") || W.undisclosed], [W.country_basis, location], [W.date, event.date], [W.first_seen, event.first_seen.slice(0, 10)], [W.push_date, event.push_date || (baseline ? W.baseline : event.next_push_label)]];
  if (website) values.splice(6, 0, [W.homepage, website]);
  return section(W.key_fields, `<dl class="kv">${values.map(([k, v]) => {
    const field = k === W.country_basis ? W.country : k;
    const basis = verification.filter(b => b.field === field || (k === W.country_basis && ["国家", "城市"].includes(b.field)));
    return `<dt>${esc(k)}</dt><dd>${k === W.homepage ? external(v, W.homepage) : linkedText(v, entities)}${basis.map(b => `<p class="muted">${esc(b.text)}${b.reason ? ` · ${esc(b.reason)}` : ""}${b.urls.map((url, i) => ` ${external(url, b.quotes[i] || W.source_label)}`).join("")}</p>`).join("")}</dd>`;
  }).join("")}</dl>`);
}
function verifySourcesHtml(company) {
  return company.verification_sources?.length ? section(W.verify_sources, company.verification_sources.map(s => `<p>${external(s.url, s.quote || W.source_label)}</p>`).join("")) : "";
}

function detailHtml(data, id) {
  useWording(data);
  const company = data.companies.find(c => c.id === id);
  if (!company) return empty(W.no_company);
  const event = data.events.find(e => e.id === company.event_id), deep = company.deepdive;
  if (company.baseline_unpushed) {
    return `<section class="section"><h1>${esc(company.header.name)}${company.backfill ? `<span class="backfill">${esc(W.supplement)}</span>` : ""}｜${esc(company.header.country || W.country_pending)}</h1>${tags(company.header.tags)}<p class="sub">${esc(W.baseline)}</p></section>` +
      keyFieldsHtml(event, [], true, company.verification, company.website) + section(W.all_sources, sourceList(company.sources)) + verifySourcesHtml(company);
  }
  const entities = [{name:company.header.name, url:company.website}, ...deep.entities, ...deep.items.flatMap(i => i.points.flatMap(p => p.entities))];
  let html = `<section class="section"><h1>${company.website ? external(company.website, company.header.name) : esc(company.header.name)}${marks(company.header.marks)}${company.backfill ? `<span class="backfill">${esc(W.supplement)}</span>` : ""}｜${esc(company.header.country || W.country_pending)}</h1><div>${tags(company.header.tags)}</div></section>`;
  if (company.identity_note) html += `<div class="alert">${esc(company.identity_note)}</div>`;
  const checks = (company.verification || []).filter(v => !v.correction);
  if (checks.length) html += section(W.verification, checks.map(v => `<div class="mini">${esc(v.field)} · ${esc(v.text)}${v.reason ? ` · ${esc(v.reason)}` : ""}${v.urls.map((url, i) => `<p>${external(url, v.quotes[i] || W.source_label)}</p>`).join("")}</div>`).join(""));
  if (deep.new_evidence.length) html += `<section class="alert">${esc(W.evidence)}${esc([...new Set(deep.new_evidence.map(e => e.field))].join("、"))}${deep.new_evidence.map(e => evidenceHtml(e.evidence)).join("")}</section>`;
  html += section(W.description, `<p>${linkedText(company.summary || W.missing, entities)}</p>`);
  if (deep.status !== W.skipped) {
    html += keyFieldsHtml(event, entities, false, company.verification, company.website);
    html += deepHtml(deep);
    html += section(W.history, `<div class="history">${company.history.map(eid => {
      const e = data.events.find(e => e.id === eid);
      return `<div><span class="sub">${esc(e.yc_batch ? `${e.yc_batch}（YC）` : e.date)}</span><p>${esc(e.round)} · ${esc(e.amount || W.amount_missing)} · ${linkedText(leadText(e), entities)}</p></div>`;
    }).join("")}</div>`);
    if (company.conflicts.length) html += section(W.conflicts, company.conflicts.map(c => `<div class="alert">${esc(c.field)}：${c.values.map(v => `${external(v.url, v.name)} 写 ${esc(v.value)}`).join("，")}</div>`).join(""));
  }
  return html + section(W.all_sources, sourceList(company.sources)) + verifySourcesHtml(company);
}
function fetchRow(source) {
  return `<p class="muted">${esc(W.fetch_time)} ${esc(source.last_at || W.not_fetched)} · ${source.count} 条 · ${esc(W.latest)} ${esc(source.latest_at || W.unknown)}</p><div class="${[W.success, W.stopped].includes(source.state) ? "muted" : "bad-text"}">${esc(W.result)}${esc(source.result)}</div><div class="technical">${esc(W.technical)}${esc(source.technical || W.unknown)}</div>`;
}
function sourcesHtml(data) {
  useWording(data);
  const status = data.status;
  let html = (data.week.baseline_notice ? `<div class="alert">${esc(data.week.baseline_notice)}</div>` : "") + section(phrase("connected", {count:status.sources.length}), status.sources.map(s => `<details class="source-row"><summary>${esc(s.name)} <span class="${s.state === W.success ? "good" : "bad-text"}">${esc(s.state === W.success ? W.normal : s.state)}</span></summary>${s.addresses.map(a => `<div class="source-row">${external(a.url, W.address)}${fetchRow(a)}</div>`).join("")}</details><div class="muted">${esc(s.name)} · ${esc(W.fetch_time)} ${esc(s.last_at || W.not_fetched)} · ${s.count} 条 · ${esc(W.latest)} ${esc(s.latest_at || W.unknown)}</div><div class="muted">${esc(W.result)}${esc(s.result)}</div><div class="technical">${esc(W.technical)}${esc(s.technical || W.unknown)}</div>`).join(""));
  if (status.fx) html += section(W.fx, `<p class="${status.fx.ok ? "muted" : "bad-text"}">${esc(status.fx.ok && status.fx.date ? phrase("fx_date", {day:status.fx.date.slice(5)}) : status.fx.message)}</p><div class="sub">${external(status.fx.source, W.ecb)}</div>`);
  if (status.verification) html += section(W.verification, `<p>${esc(phrase("verify_stats", status.verification))}</p>`);
  if (status.homepage) {
    const h = status.homepage;
    html += section(W.homepage, (h.has_digest ? `<p class="${h.low ? "alert" : "muted"}">${esc(phrase("homepage_coverage", h))}</p>` : "") + (h.missing.length ? `<p>${esc(phrase("homepage_missing", {count:h.missing.length, names:h.missing.map(c => c.name).join("、")}))}</p>` : "") + `<p>${esc(phrase("homepage_usage", {searches:h.searches, cost:Number(h.month_used).toFixed(4)}))}</p>`);
  }
  if (status.missed.length) html += `<div class="alert bad">${status.missed.map(m => esc(phrase("missed", {name:m.name, span:m.range}))).join("；")}</div>`;
  const ratios = status.pending_ratios;
  if (ratios) html += section(phrase("ratios", {date:ratios.date}), `<dl class="kv">${[["country", W.country_pending], ["round", W.round_pending], ["tag", W.tag_pending]].map(([k, label]) => `<dt>${label}</dt><dd>${esc(phrase("ratio", {percent:ratios[k].percent, total:ratios.denominator, count:ratios[k].count}))}</dd>`).join("")}</dl>`);
  const p = status.deepdive_progress;
  html += section(W.deep, `<p class="sub">${esc(p.date)}</p><dl class="kv">${[[W.due, p.total], [W.done, p.done], [W.running, p.running], [W.failed, p.failed], [W.absent, p.timeout], [W.skipped, p.skipped]].map(([label, n]) => `<dt>${label}</dt><dd>${esc(phrase("companies_count", {count:n}))}</dd>`).join("")}</dl>`);
  const money = v => v === null ? W.no_record : `¥${Number(v).toFixed(4)}`;
  html += section(W.month_cost, `<dl class="kv"><dt>${esc(W.deep)}</dt><dd>${money(status.deepdive_budget.month_used)}</dd><dt>${esc(W.extraction)}</dt><dd>${money(status.extract_budget.month_used)}</dd></dl>`);
  if (status.verification) html += section(W.verification, `<p>${money(status.verification.month_used)}</p><p>${esc(phrase("verify_cost", {first:status.verification.first_issue_cost, backfill:status.verification.backfill_cost}))}</p>`);
  html += section(W.evidence_companies, status.new_evidence.length ? status.new_evidence.map(e => `<div class="mini"><a href="${companyRoute(e.company_id)}">${esc(e.name)}</a> · ${esc(e.fields.join("、"))}<div class="sub">${esc(e.found_at)}</div></div>`).join("") : `<p class="muted">${esc(W.no_evidence)}</p>`);
  if (status.quality_pending.length) html += section(W.quality, status.quality_pending.map(c => `<div class="mini"><a href="${companyRoute(c.company_id)}">${esc(c.name)}</a></div>`).join(""));
  return html + section(W.not_connected, status.not_connected.map(s => `<div class="mini">${esc(s.name)}<div class="muted">${esc(s.reason)}</div></div>`).join(""));
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
        return detail;
      }).catch(error => { requests.delete(id); throw error; });
      requests.set(id, request);
    }
    return requests.get(id);
  };
}

if (typeof module !== "undefined") module.exports = {selectEvents, filtersForRoute, defaultFilters, external, linkedText, weekHtml, listHtml, detailHtml, sourcesHtml, ycHtml, createDetailLoader};
if (typeof document !== "undefined") {
  let data, loadDetail, renderVersion = 0, lastHash;
  const filters = defaultFilters();
  async function render() {
    if (!data) return;
    const version = ++renderVersion;
    const hash = location.hash || "#week", isDetail = hash.startsWith("#c/");
    filtersForRoute(hash, lastHash, filters);
    lastHash = hash;
    const route = isDetail || hash === "#backfill" ? "list" : hash === "#yc" ? "yc" : ["#week", "#list", "#sources"].includes(hash) ? hash.slice(1) : "week";
    document.querySelectorAll("nav a").forEach(a => {
      a.classList.toggle("on", a.dataset.nav === (route === "yc" ? "week" : route));
      if (a.dataset.nav === (route === "yc" ? "week" : route)) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    let title = {week:W.product, list:W.archive, sources:W.sources, yc:W.yc}[route];
    let id;
    if (isDetail) {
      try { id = decodeURIComponent(hash.slice(3)); } catch (_) { id = ""; }
      title = data.companies.find(c => c.id === id)?.header?.name || data.events.find(e => e.company_id === id)?.name || W.company_detail;
    }
    document.title = `${title} · ${W.product}`;
    document.getElementById("header").innerHTML = `${isDetail ? `<a class="back" href="#list">${esc(W.back)}</a>` : ""}<h1>${esc(title)}</h1>${route === "week" ? `<div class="sub">${esc(W.subtitle)}</div>` : ""}`;
    let html;
    if (isDetail) {
      document.getElementById("main").innerHTML = empty(W.loading_detail);
      try {
        const detail = await loadDetail(id);
        html = detail ? detailHtml(detail, id) : empty(W.no_company);
      } catch (_) {
        html = empty(W.detail_failed) + `<button type="button" id="retry-detail">${esc(W.retry)}</button>`;
      }
      if (version !== renderVersion) return;
    } else html = route === "list" ? listHtml(data, filters) : route === "sources" ? sourcesHtml(data) : route === "yc" ? ycHtml(data) : weekHtml(data);
    document.getElementById("main").innerHTML = html;
  }
  document.addEventListener("click", e => {
    if (e.target.closest("#retry-detail")) { render(); return; }
    if (e.target.closest("[data-clear-backfill]")) { Object.assign(filters, defaultFilters()); render(); return; }
    if (e.target.closest("[data-backfill-mark]")) { filters.backfill = !filters.backfill; render(); return; }
    const button = e.target.closest("[data-filter]");
    if (button) { filters[button.dataset.filter] = button.dataset.value; render(); }
  });
  document.addEventListener("change", e => {
    if (e.target.id === "issue" && e.target.value) { filters.period = e.target.value; render(); }
  });
  window.addEventListener("hashchange", () => { render(); window.scrollTo(0, 0); });
  fetch("./data.json").then(r => { if (!r.ok) throw new Error(); return r.json(); }).then(value => {
    data = value; useWording(data); loadDetail = createDetailLoader(data); render();
  }).catch(() => {
    const main = document.getElementById("main");
    main.innerHTML = empty(W.load_failed || main.dataset?.loadFailed);
  });
}
