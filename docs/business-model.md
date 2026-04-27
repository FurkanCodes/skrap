# Business Model

*Last updated: 2026-04-20*

## Summary

Skrap should launch with a hybrid business model:

- `Free` is explicitly monetized by affiliate links in alerts and acts as the acquisition funnel.
- `Plus` and `Pro` are subscription plans for serious deal hunters who want more trackers and faster checks.
- Credits are the real usage budget behind every paid plan.

This keeps the pricing simple for users while matching the backend's current entitlement model:

- `max_active_trackers`
- `min_check_interval_minutes`
- `monthly_check_units`
- `allowed_channels`
- `overage_policy`

## Core Pricing Decision

### What a credit is

At launch:

- `1 credit = 1 scheduled product re-check`
- Credits are consumed by background tracking checks, not by browsing the UI
- Manual one-off scrapes are top-of-funnel and should be rate-limited separately, not billed as credits

Reason:

- This maps directly to the existing `monthly_check_units` field
- It is easy to explain
- It avoids premature complexity around weighted costs by fetch mode

Future option, not launch:

- If stealth or dynamic scraping proves materially more expensive, we can later introduce weighted credits per check mode

### How plans work

Every plan has three limits:

1. How many active products you can track at once
2. How fast those products can be checked
3. How many total monthly re-checks you get

That means users can trade breadth for speed:

- Track many items at slower intervals
- Or track fewer items at faster intervals

## Locked Launch Plans

### Free

- Price: `$0`
- Active trackers: `5`
- Monthly credits: `100`
- Fastest interval: `720 minutes`
- Channels: `Telegram`
- Overage policy: `Block`
- Monetization: affiliate links allowed in notifications

Who it is for:

- New users validating that Skrap works for their stores and products
- Casual shoppers tracking a small watchlist

### Plus

- Price: `$9/month` or `$90/year`
- Active trackers: `25`
- Monthly credits: `10,000`
- Fastest interval: `120 minutes`
- Channels: `Telegram`
- Overage policy: `Block`
- Monetization: subscription revenue first; affiliate behavior on this tier remains open

Who it is for:

- Power shoppers
- Users tracking multiple products across multiple stores
- People who want meaningful automation without enterprise-style complexity

### Pro

- Price: `$19/month`
- Active trackers: `100`
- Monthly credits: `50,000`
- Fastest interval: `30 minutes`
- Channels: `Telegram`
- Overage policy: `Block`
- Monetization: subscription revenue first; affiliate behavior on this tier remains open

Who it is for:

- Heavy deal hunters
- Resellers and arbitrage users
- Users who want fast checks across large watchlists

## Business Model Rules

### Credits

- Credits reset every billing cycle
- Unused credits do not roll over
- Credits are shared across all active trackers in the account
- When credits are exhausted, scheduled checks stop until the next cycle or until the user upgrades

### Overage

Launch decision:

- No pay-as-you-go overages
- No separate top-up packs at launch
- Hard stop on exhausted credits

Reason:

- Matches the current backend shape (`overage_policy = block`)
- Keeps billing and support simple
- Makes upgrade pressure clear instead of creating small, confusing add-on purchases

### Affiliate revenue

Launch decision:

- Keep affiliate monetization on `Free`
- Do not promise affiliate-free paid plans yet

Reason:

- Free users can subsidize acquisition
- Paid conversion should come from speed, scale, and convenience first
- The backend already supports affiliate URLs in notification payloads
- We can test whether affiliate links should remain on some paid traffic without rewriting the pricing model

## Positioning

Skrap is not selling "scrapes." It is selling continuous market watching.

The message should be:

- "Track prices automatically"
- "Get notified when deals appear"
- "Choose how broad or how fast you want monitoring to be"

Not:

- "Buy X API calls"
- "Buy X crawler runs"

Credits should be visible, but secondary to outcome-based language.

## Customer-Facing Explanation

Use this wording in the product:

> Every plan includes a monthly pool of tracking credits. One credit equals one product re-check. Faster tracking uses credits more quickly, while slower tracking lets you cover more products with the same plan.

Short version:

> More products or faster checks. Same credit pool.

## Why This Model Fits The Product

### It matches the real cost driver

The expensive part of Skrap is repeated checking, not account creation or static storage.

### It matches the current backend

The backend already models:

- tracker count caps
- minimum interval caps
- monthly usage units
- blocked overages

### It keeps the free plan viable

Free users can still convert through:

- affiliate revenue
- upgrade pressure from tracker limits
- upgrade pressure from faster-check needs

## Launch Non-Goals

Do not add these before launch:

- Team plans
- Seat-based billing
- Pay-as-you-go usage billing
- Complex weighted credit pricing
- Channel-based pricing promises beyond Telegram until those channels exist

## Backend Mapping

This is the direct mapping to existing entitlement fields:

| Plan | `plan_key` | `max_active_trackers` | `min_check_interval_minutes` | `monthly_check_units` | `allowed_channels` | `overage_policy` |
|---|---|---:|---:|---:|---|---|
| Free | `free` | 5 | 720 | 100 | `telegram` | `block` |
| Plus | `plus` | 25 | 120 | 10000 | `telegram` | `block` |
| Pro | `pro` | 100 | 30 | 50000 | `telegram` | `block` |

## Product Decisions To Implement Next

1. Enforce monthly credit consumption in the scheduler using `usage_counters`
2. Expose remaining monthly credits in `/api/v1/me`
3. Add a way to assign entitlements from subscription state
4. Decide later whether affiliate-link behavior should vary by plan, market, or campaign

## Final Call

The launch business model is:

- `Free` for adoption and clear affiliate monetization
- `Plus` for mainstream paid conversion
- `Pro` for heavy monitoring users
- Credits as a simple monthly re-check budget
- Hard-stop overages instead of top-ups

This is the model we should build against unless product data forces a pricing change later.
