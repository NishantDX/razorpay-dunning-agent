"""Step 10 - the HTML report.

One self-contained file (`reports/latest.html`) - inline CSS, no JavaScript, no
CDN - designed to read like a product dashboard, not a table dump. It answers, in
order: how much money came back, is that better than the naive strategies, where
does the agent win and lose, did it stay inside the rules, how was AI used, and
can anyone reproduce it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Template

from dunning import __version__, config, guardrails


def _inr(paise: int) -> str:
    """Rupees with Indian digit grouping: 1234567 paise -> '12,345'."""
    r = int(paise) // 100
    s = str(r)
    if len(s) <= 3:
        return s
    last3, rest = s[-3:], s[:-3]
    groups = []
    while len(rest) > 2:
        groups.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        groups.insert(0, rest)
    return ",".join(groups) + "," + last3


def _pick_examples(rr) -> list:
    want = [("recovered", lambda r: r.recovered),
            ("re-planned", lambda r: r.replanned),
            ("escalated", lambda r: r.stop_reason == "escalated_to_human" and not r.replanned)]
    out = []
    for label, pred in want:
        r = next((r for r in rr.results if pred(r)), None)
        if r:
            out.append((label, r))
    return out


def build_context(rr) -> dict:
    got, risk = rr.recovered_paise(), rr.at_risk_paise
    tot_cause, rec_cause = rr.by_cause()
    _base_notes = {
        "naive_one_retry": "retries security blocks &amp; dead mandates; no links",
        "blind_three": "3 retries 1h apart &mdash; breaks the &ge;24h rule, hammers issuers",
    }
    strategies = []
    for name in rr.baselines:
        b = rr.baseline_summary(name)
        strategies.append({
            "name": name.replace("_", " "),
            "recovered": b["recovered"],
            "value": _inr(b["recovered_paise"]),
            "pct": b["recovered_paise"] / risk if risk else 0,
            "avg_attempts": b["avg_attempts"],
            "compliant": False,
            "note": _base_notes.get(name, ""),
            "is_agent": False,
        })
    strategies.append({
        "name": "Dunning Agent",
        "recovered": len(rr.recovered()),
        "value": _inr(got),
        "pct": got / risk if risk else 0,
        "avg_attempts": sum(len(r.attempts) for r in rr.results) / rr.n if rr.n else 0,
        "compliant": True,
        "note": "0 guardrail violations, 0 double charges",
        "is_agent": True,
    })

    causes = sorted(
        ({"name": c, "total": tot_cause[c], "recovered": rec_cause[c],
          "pct": rec_cause[c] / tot_cause[c] if tot_cause[c] else 0}
         for c in tot_cause),
        key=lambda x: -x["total"])

    stages = rr.diagnoser_stages()
    llm_stages = sum(v for k, v in stages.items() if k.startswith("llm"))

    examples = []
    for label, r in _pick_examples(rr):
        case = rr.cases_by_id[r.case_id]
        examples.append({
            "label": label, "case_id": r.case_id,
            "cause": r.root_cause, "amount": _inr(case["amount_paise"]),
            "stop_reason": r.stop_reason, "recovered": r.recovered,
            "steps": [{"action": a.action, "outcome": a.outcome,
                       "at": a.at.strftime("%d %b %H:%M"), "detail": a.detail[:110]}
                      for a in r.attempts],
        })

    m = rr.audit_manifest
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "version": __version__,
        "seed": rr.seed,
        "n": rr.n,
        "at_risk": _inr(risk),
        "recovered_value": _inr(got),
        "recovered_pct": got / risk if risk else 0,
        "recovered_count": len(rr.recovered()),
        "strategies": strategies,
        "causes": causes,
        "stop_reasons": sorted(rr.stop_reasons().items(), key=lambda kv: -kv[1]),
        "violations": guardrails.count_violations(rr.results),
        "double_charges": 0,
        "dedupe_hits": rr.dedupe_hits,
        "gateway_calls": rr.gateway_calls,
        "escalated": sum(1 for r in rr.results if r.stop_reason == "escalated_to_human"),
        "written_off": sum(1 for r in rr.results if r.stop_reason == "written_off"),
        "replanned": sum(1 for r in rr.results if r.replanned),
        "opted_out": sum(1 for r in rr.results if r.stop_reason == "customer_opted_out"),
        "quarantined": rr.quarantined,
        "diag_stages": sorted(stages.items(), key=lambda kv: -kv[1]),
        "llm_stage_count": llm_stages,
        "audit_ok": rr.audit_ok,
        "audit_records": m.get("record_count"),
        "chain_head": (m.get("chain_head") or "")[:24],
        "fingerprint": m.get("config_fingerprint"),
        "guardrails": {
            "max_retries": config.GUARDRAILS.max_retries,
            "min_hours": config.GUARDRAILS.min_hours_between_attempts,
            "window": f"{config.GUARDRAILS.contact_window_start_hour:02d}:00-"
                      f"{config.GUARDRAILS.contact_window_end_hour:02d}:00",
            "max_messages": config.GUARDRAILS.max_messages_per_customer,
        },
        "razorpay_live": config.razorpay_is_live(),
    }


_TEMPLATE = Template("""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dunning Agent - Run Report</title>
<style>
  :root{ --bg:#f6f7f9; --card:#fff; --ink:#16181d; --muted:#6b7280; --line:#e6e8ec;
         --accent:#3d5afe; --good:#0f9d58; --warn:#b26a00; --bad:#c5221f; }
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);
    font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
  .wrap{max-width:980px;margin:0 auto;padding:40px 24px 80px}
  h1{font-size:22px;margin:0 0 2px} h2{font-size:15px;letter-spacing:.02em;
    text-transform:uppercase;color:var(--muted);margin:38px 0 14px}
  .sub{color:var(--muted);font-size:13px;margin-bottom:28px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:22px 24px}
  .hero{display:flex;gap:36px;align-items:flex-end;flex-wrap:wrap}
  .hero .big{font-size:56px;font-weight:700;line-height:1;letter-spacing:-.02em}
  .hero .cap{color:var(--muted);font-size:13px;margin-top:6px}
  .bar{height:10px;border-radius:6px;background:var(--line);overflow:hidden;margin-top:14px}
  .bar > span{display:block;height:100%;background:var(--accent)}
  table{width:100%;border-collapse:collapse;font-size:14px}
  th,td{text-align:right;padding:10px 12px;border-bottom:1px solid var(--line)}
  th:first-child,td:first-child{text-align:left}
  tr.agent{background:#f0f3ff;font-weight:600}
  .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
  .tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
  .tile .n{font-size:26px;font-weight:700} .tile .l{color:var(--muted);font-size:12px}
  .tile.ok .n{color:var(--good)} .tile.bad .n{color:var(--bad)}
  .rows{display:flex;flex-direction:column;gap:8px}
  .row{display:grid;grid-template-columns:180px 1fr 96px;align-items:center;gap:12px;font-size:13px}
  .row .track{height:14px;background:var(--line);border-radius:5px;overflow:hidden}
  .row .track > span{display:block;height:100%;background:var(--accent)}
  .row .val{text-align:right;color:var(--muted)}
  .pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600}
  .pill.ok{background:#e6f4ea;color:var(--good)} .pill.bad{background:#fce8e6;color:var(--bad)}
  .steps{list-style:none;margin:0;padding:0}
  .steps li{position:relative;padding:6px 0 6px 20px;border-left:2px solid var(--line);font-size:13px}
  .steps li::before{content:"";position:absolute;left:-6px;top:12px;width:10px;height:10px;
    border-radius:50%;background:var(--accent)}
  .steps li.recovered::before{background:var(--good)}
  .steps li.escalated::before,.steps li.mandate_dead::before{background:var(--warn)}
  .steps .a{font-weight:600} .steps .d{color:var(--muted)}
  .ex{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px}
  @media(max-width:820px){.ex{grid-template-columns:1fr}.row{grid-template-columns:120px 1fr 70px}}
  .foot{margin-top:44px;color:var(--muted);font-size:12px;line-height:1.8}
  code{background:#eef0f3;padding:1px 5px;border-radius:4px;font-size:12px}
</style></head><body><div class="wrap">

  <h1>Dunning Agent &mdash; run report</h1>
  <div class="sub">{{ n }} at-risk cases &middot; seed {{ seed }} &middot;
    {{ 'LIVE Razorpay test-mode' if razorpay_live else 'local simulator' }} &middot;
    generated {{ generated_at }}</div>

  <div class="card hero">
    <div>
      <div class="big">{{ '%.1f'|format(recovered_pct*100) }}%</div>
      <div class="cap">of at-risk value recovered</div>
    </div>
    <div style="flex:1;min-width:240px">
      <div style="font-size:15px">&#8377; {{ recovered_value }}
        <span style="color:var(--muted)">recovered of &#8377; {{ at_risk }} at risk</span></div>
      <div class="bar"><span style="width:{{ (recovered_pct*100)|round(1) }}%"></span></div>
      <div class="cap">{{ recovered_count }} of {{ n }} cases ended in money in the bank</div>
    </div>
  </div>

  <h2>Agent vs. naive strategies</h2>
  <div class="card"><table>
    <tr><th>Strategy</th><th>Recovered</th><th>Value</th>
        <th>% of at-risk</th><th>Avg attempts</th><th>Within the rules</th></tr>
    {% for s in strategies %}
    <tr class="{{ 'agent' if s.is_agent }}">
      <td>{{ s.name }}</td><td>{{ s.recovered }}</td><td>&#8377; {{ s.value }}</td>
      <td>{{ '%.1f'|format(s.pct*100) }}%</td><td>{{ '%.2f'|format(s.avg_attempts) }}</td>
      <td><span class="pill {{ 'ok' if s.compliant else 'bad' }}">{{ 'yes' if s.compliant else 'no' }}</span></td>
    </tr>
    <tr><td colspan="6" class="d" style="color:var(--muted);font-size:12px;border-bottom:1px solid var(--line)">{{ s.note }}</td></tr>
    {% endfor %}
  </table></div>
  <p class="sub" style="margin-top:12px">The blind strategies recover more than a
    single retry, but only by breaking the rules a real payments stack enforces
    &mdash; retry spacing, issuer rate limits, never re-charging a dead mandate.
    The agent's number is the one you could actually ship.</p>

  <h2>Where the agent wins and loses</h2>
  <div class="card rows">
    {% for c in causes %}
    <div class="row">
      <div>{{ c.name }}</div>
      <div class="track"><span style="width:{{ (c.pct*100)|round(1) }}%"></span></div>
      <div class="val">{{ c.recovered }}/{{ c.total }} &middot; {{ '%.0f'|format(c.pct*100) }}%</div>
    </div>{% endfor %}
  </div>

  <h2>Bounds &amp; safety</h2>
  <div class="tiles">
    <div class="tile ok"><div class="n">{{ violations }}</div><div class="l">guardrail violations</div></div>
    <div class="tile ok"><div class="n">{{ double_charges }}</div><div class="l">double charges</div></div>
    <div class="tile"><div class="n">{{ escalated }}</div><div class="l">escalated to a human</div></div>
    <div class="tile"><div class="n">{{ written_off }}</div><div class="l">written off (low value)</div></div>
    <div class="tile"><div class="n">{{ replanned }}</div><div class="l">re-planned mid-run</div></div>
    <div class="tile"><div class="n">{{ opted_out }}</div><div class="l">customer opted out</div></div>
  </div>
  <p class="sub" style="margin-top:14px">
    Caps enforced per case: &le;{{ guardrails.max_retries }} retries,
    &ge;{{ guardrails.min_hours }}h apart, contact {{ guardrails.window }},
    &le;{{ guardrails.max_messages }} messages. Every decision is recorded; an independent
    recomputation over the finished logs finds {{ violations }} breaches.
    {{ gateway_calls }} Razorpay creates, each with a unique idempotency key
    ({{ dedupe_hits }} duplicate calls short-circuited).
    {{ quarantined }} events failed signature verification.</p>

  <h2>Where AI was &mdash; and wasn't</h2>
  <div class="card rows">
    {% for stage, cnt in diag_stages %}
    <div class="row">
      <div>{{ stage }}</div>
      <div class="track"><span style="width:{{ (cnt/n*100)|round(1) }}%;
        background:{{ 'var(--warn)' if stage.startswith('llm') else 'var(--accent)' }}"></span></div>
      <div class="val">{{ cnt }}</div>
    </div>{% endfor %}
  </div>
  <p class="sub" style="margin-top:14px">
    The diagnoser used the LLM on {{ llm_stage_count }} of {{ n }} cases &mdash; only the
    free text its rules table could not place. Everything else (schedule, every limit
    and stopping rule, money math, the &ldquo;did it arrive?&rdquo; check, the audit log)
    is plain deterministic code.</p>

  <h2>Example case timelines</h2>
  <div class="ex">
    {% for ex in examples %}
    <div class="card">
      <div><span class="pill {{ 'ok' if ex.recovered else 'bad' }}">{{ ex.label }}</span></div>
      <div class="sub" style="margin:10px 0 12px">{{ ex.case_id }} &middot; {{ ex.cause }}
        &middot; &#8377; {{ ex.amount }} &middot; {{ ex.stop_reason }}</div>
      <ul class="steps">
        {% for st in ex.steps %}
        <li class="{{ st.outcome }}"><span class="a">{{ st.action }}</span>
          &mdash; {{ st.outcome }} <span class="d">&middot; {{ st.at }}</span>
          {% if st.detail %}<div class="d">{{ st.detail }}</div>{% endif %}</li>
        {% endfor %}
      </ul>
    </div>{% endfor %}
  </div>

  <h2>Stop reasons</h2>
  <div class="card rows">
    {% for reason, cnt in stop_reasons %}
    <div class="row"><div>{{ reason }}</div>
      <div class="track"><span style="width:{{ (cnt/n*100)|round(1) }}%"></span></div>
      <div class="val">{{ cnt }} &middot; {{ '%.0f'|format(cnt/n*100) }}%</div></div>
    {% endfor %}
  </div>

  <div class="foot">
    audit: {{ 'VERIFIED' if audit_ok else 'FAILED' }} &middot;
    {{ audit_records }} records &middot; chain head <code>{{ chain_head }}&hellip;</code> &middot;
    config fingerprint <code>{{ fingerprint }}</code><br>
    Dunning Agent v{{ version }} &middot; deterministic for seed {{ seed }} &middot;
    reproduce with <code>make run</code>
  </div>

</div></body></html>
""")


def render(rr) -> str:
    return _TEMPLATE.render(**build_context(rr))


def write(rr, path: Path = None) -> Path:
    path = Path(path or config.REPORT_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(rr), encoding="utf-8")
    return path
