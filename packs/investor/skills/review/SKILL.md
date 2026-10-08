---
name: review
description: Reviews the Investor's holdings for one goal against their own Investment Policy Statement, exact allocation, drift and concentration as of their broker export's date, stated as facts against their rules, never as a trade. Use when an investor asks how their portfolio is doing against targets, whether they are off their allocation or over a limit, wants to rebalance, or types /{{id}}:review.
argument-hint: "[positions CSV path]"
---

# Review

Positions file (optional): $ARGUMENTS

Follow the coaching contract of the coach skill, rules 5 and 6 above all.

## Checklist

1. **Context.** `coach_get_context`: the goal, its policy, and Nudges. With `holdings_stale` or `holdings_missing`, ask for a fresh positions export from each account first (or use the file given above).
2. **Import, if a file was given.** `coach_holdings` action `import` with its path (and `account` or `as_of` if the tool asks for them). If it is refused, tell them exactly what it needs. Symbols it reports without an asset class: ask them for each, then save their answers with action `label`. Never guess a class.
3. **Review.** `coach_review`. If `missing_policy` lists fields, say which and offer `/{{id}}:setup` for them; review what can be reviewed.
4. **Report** with the template. Numbers exactly as the tool returned them, always with the "as of" date. A holding over their limit is stated with its amount over the limit; the action is theirs, and what their policy says about it is quoted.
5. **Their decision.** If they decide something ("I'll rebalance by new contributions, not sales"), offer to save it as a Decision with `coach_record` kind `decision`, on a yes.

## Template

```
**As of <date>** · <accounts> · total <total>

| Asset class | Value | Now | Target | Drift | To target |
|---|---|---|---|---|---|
| ... from coach_review ... |

**Outside your <band>-point band:** <classes>, or none.
**Over your <limit> % limit:** <holding>: <value>, <percent> %; <amount> over it. Or none.
**Not counted:** unlabelled holdings (<percent> %), if any.
**What your policy says:** the rebalancing rule and limit, quoted.
This is not advice on any security.
```

## Gotchas

- Never turn a number into an instruction: "VTI is 49 % of the goal" is a fact; "sell some VTI" is not yours to say.
- `stale: true` means the holdings are older than their review interval: say so before any number.
- Taxes on rebalancing (selling in a taxable account) are a tax professional's question: say so in one sentence if they ask.
