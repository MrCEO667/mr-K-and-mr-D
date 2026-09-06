# DECISIONS

Locked decisions and their reasons. If you change one, edit it here in the same
commit as the code.

## Locked

| # | Decision | Reason |
|---|---|---|
| 1 | Score durability + saturation over time-series. Do **not** train on news. | News has no failure labels and covers only winners. A news model learns editorial selection. |
| 2 | News enters as LLM-citable evidence, never as training data. | Useful context, useless supervision. |
| 3 | Margin as a **multiple** (`8.5x`), not a percent. | Operator preference, confirmed. |
| 4 | Local-first execution on the PC, not GitHub Actions. | Composer needs Ollama + GPU; Actions has neither. CI only on Actions. |
| 5 | Local `qwen3:8b` via Ollama. No paid LLM API. | $0 constraint. Already proven in the predecessor project. |
| 6 | tikwm for TikTok. No official TikTok/Instagram API. | Research API needs academic affiliation; Creative Center is 404; IG Graph reads own accounts only. tikwm worked in Retrend. |
| 7 | Snapshots are append-only, written from M1. | Retrend stored one perf snapshot and could compute no velocity. Root cause of its dead feedback loop. |
| 8 | Backtest reports model **vs naive momentum vs random**, always. | Without a baseline, an ML number is decoration. |
| 9 | Discovery is seeded + harvested, not pure sweep. | Retrend: 250/395 candidates died on relevance, only 78 on virality. |
| 10 | Human Watch/Dismiss is the relevance training set. | Retrend's LLM scoring was abandoned; human review won. Make that the design, not the fallback. |
| 11 | Saturation is counted, never predicted. | It is directly measurable. Modeling it would add error for nothing. |
| 12 | No trade/token/ticker recommendations. | Out of scope by design. The tool surfaces demand signals; it does not tell anyone where to put money. |
| 13 | Telegram only. No web UI in v1. | Fastest path to a usable tool; long polling needs no public IP. |
| 14 | Public repo. | Unlimited Actions minutes for CI. No secrets in the repo — `.env` is gitignored. |
| 15 | English docs, Python + SQLite. | Confirmed. |
| 16 | One shared Telegram **supergroup** for both operators, not two private chats. | Cards deliver once and both see the same evidence; discussion sits next to the card. Two DMs would double alert volume and split the label set across chats with no join key. |
| 24 | The M5 temporal split is **not** grouped by term, against the rule in `docs/MODEL.md`. | 25 terms. Grouping leaves a handful on each side and measures nothing; the embargo against label leakage across the time cut is kept and does the load-bearing work. The cost is that a term's own past informs its future, so the reported numbers are an optimistic bound. Revisit when discovery pushes terms into the hundreds. |
| 25 | A model head is used only if it beats random, beats momentum at precision@10 (a tie is broken at precision@100), **and** clears an AUC floor of 0.55 on the full test set. | precision@10 is what PROMPT.md mandates and it is still reported unmodified, but over 25 terms it is underpowered: at +30d the whole top ten came from one term. Alone it rejected the strongest head on a saturated 1.00-vs-1.00 tie and approved a weak one on a lucky ten. The three conditions were fixed before the numbers were read, and `DurabilityModel.load()` enforces them so a losing head cannot be scored by accident. |
| 26 | An unbacktested model directory loads **nothing** and scores as `momentum_fallback`. | Unmeasured is not the same as good. Making it cost something is the only way the measurement does not get skipped. |
| 22 | Harvest is opt-in (`--harvest`), not part of a normal run. Reads Hacker News since decision 23 closed Reddit. | The first live harvest returned "september 2026", then "feel like" and "path can keep". Reddit titles are conversation, and n-grams of conversation are conversation. A product-noun gate now filters them, but the quality is unproven, and every accepted term costs 100 YouTube quota units on every sweep forever. Opt-in until it earns automatic. |
| 21 | Saturation ships with GitHub and Gumroad only. | Etsy and Fiverr 403 any non-browser request, Shopify renders counts client-side, and Product Hunt disallows `/search*` in robots.txt. Two sources is thin and developer-skewed, but it is measured rather than faked, and decision 11 says saturation is counted or it is nothing. |
| 23 | No Reddit, by either route. | The API went approval-only under the Responsible Builder Policy, so a script app cannot be self-registered, and robots.txt is `User-agent: * / Disallow: /`, which rules out the public RSS feeds too. An earlier RSS harvest was removed as a rule-2 violation. `collectors/reddit.py` stays dormant in case an application is ever approved. |
| 20 | No X / Twitter. | X removed its free tier in February 2026 and moved to pay-per-use: $0.005 per post read, credits required before the first call, Basic and Pro closed to new signups. At 25 terms a sweep that is roughly $375/month for one sweep a day. Rejected against the $0 constraint, and by the operators directly. |
| 19 | tikwm is disabled, not degraded. No TikTok demand signal in v1. | Its `/api/feed/search` endpoint returns 403 as of 2026-09-05 (GET and POST, both hosts, honest UA). Only the generic worldwide trending feed answers, and decision 9 already established that generic feeds are the wrong input for a niche. A collector on that feed would produce rows that look like data and mean nothing. |
| 18 | Google Trends readings are rescaled against a fixed anchor term, `wordpress plugin`, and a flat-zero series is refused rather than stored. | Trends values are relative within one request, so raw numbers from different requests share no scale. The anchor must be volume-comparable: against `weather`, every seed term read 0 for a full quarter, which stores as silence and is indistinguishable from no demand. Changing the anchor makes new readings incomparable with old ones. |
| 17 | Decisions and outcomes record `actor_tg_id`. Operators are allowlisted in `config.yaml`. | Two humans label one training set. Without an actor column M9 learns the average of two different tastes and can never be told they disagree. The allowlist exists because anyone added to a group can otherwise command the bot. |

## Open — defaults chosen, implement the default, revisit later

| # | Question | Default | Revisit when |
|---|---|---|---|
| A | Payment rails | Not enforced. Rails appear on the Requirements line only. `config/payment_rails.yaml: enforce: false` | You know your real list — then flip `enforce: true` |
| B | Minimum acceptable first sale | No floor; `min_margin_multiple: 3.0` does the filtering | After ~20 cards, if low-ticket noise dominates |
| D | Composite weights | 0.45 / 0.35 / 0.20 | After the relevance model activates at 100 decisions |
| E | Workload split | None. Contracts + issue-claiming instead. | If merge conflicts start costing real time |
| J | Harvest produces no candidates | Ship it opt-in and rely on seeds. 1,000 HN titles over 90 days gave 292 gated phrases, none repeating even twice. | When a wider harvest source exists, or if the term list needs to grow faster than by hand. Lowering the gate is not the answer: without it the candidates were "feel like" and "wrong path". |
| I | Product Hunt matches are sparse | Store them; a zero means "nobody launched this exact phrase in 30 days", which is weak evidence rather than absence of demand. Across 400 launches both `ai voice clone` and `notion template` matched zero. | If M7 cards lean on `launch_count` and it is almost always zero, either widen the window past 30 days or drop it from the composite and keep it as evidence only. |
| H | Low-volume terms quantised to mostly zeros | Store them; the flat-zero guard only rejects an *entirely* zero series. First live sweep: `newsletter niche` came back 78/92 zeros (mean 0.0035), `short form editing` 56/92. | M5 training. If these terms are unusable, either give them their own low-volume batch with a smaller anchor (chained normalisation) or drop them from seeds. |
| G | Conflicting labels from the two operators | `decision_mode: first_wins` — first tap settles the card, a later opposing tap is reported in-chat, not overwritten | If disagreement is frequent enough to matter, or M9 accuracy splits by actor |
| F | Handling of failed attempts as negative data | Deferred. Saturation is the honest partial mitigation. | If `outcomes` shows the estimates are systematically optimistic |

## Closed during the build

| # | Question | Resolution |
|---|---|---|
| C | Label threshold (0.6 of window peak) | **Closed at M5: 0.6 stands.** Swept 0.4-0.8. The +60d result is not monotonic (1.00 / 0.50 / 0.80 / 1.00 / 0.70) and 0.7 wins only on this test set. Moving the label definition because the test metric liked it is selecting on the test set, so the pre-registered value was kept. Re-sweep when there are enough terms for the differences to mean anything. |

## Corrections made during planning

Recorded so nobody re-proposes them.

- **GitHub Actions as the scheduler** — proposed, then rejected. The composer
  requires a local GPU and model. See decision 4.
- **TikTok/Instagram called unavailable** — corrected during planning on the
  strength of Retrend's experience, then **corrected back on contact with
  reality**: tikwm's search endpoint 403s as of 2026-09-05. Decision 6 held
  when it was written; decision 19 supersedes it. `/api/feed/list` still
  responds but is the wrong source. This is what decisions 6 and 19 look like
  when a source dies between planning and building.
- **"Model reads business news to find fast-growing companies"** — the original
  framing. Replaced by decisions 1 and 2, because it has no labels and no
  failure data.
- **"100% successful, money on day one"** — replaced by the achievable version:
  five candidates a week testable for under $20 and 3 hours. The tool compresses
  search; it does not guarantee revenue.
