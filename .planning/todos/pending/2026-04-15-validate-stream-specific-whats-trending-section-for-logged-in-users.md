---
created: 2026-04-15T18:28:01.337Z
title: Validate stream-specific What's Trending section for logged-in users
area: auth
files:
  - scraper.py:482-608
  - config/urls.yaml:51-53
---

## Problem

The homepage contains a "What's Trending" announcement/banner section. For logged-in users this section should surface content that is relevant to their enrolled stream:

- A **JEE** logged-in user should see JEE-relevant trending announcements (not NEET or Class 6-10).
- A **NEET** logged-in user should see NEET-relevant trending announcements.
- A **Class 6-10** logged-in user should see Class 6-10-relevant trending announcements.

There is currently no assertion validating this section's content against the logged-in stream. A regression could silently show the wrong stream's announcements to a user.

Note: this is distinct from the sticky banner validation todo — the sticky is a persistent CTA element, whereas "What's Trending" is an announcement/content section on the page body.

## Solution

In the authenticated scraping/validation pass for each auth session on the HOME URL:
1. Locate the "What's Trending" section (e.g. by heading text or a known data attribute).
2. Extract the visible announcement titles or labels within it.
3. Assert that the content is stream-relevant (e.g. contains "JEE" keywords for a JEE session, "NEET" for NEET, etc.) or confirm it is intentionally generic.
4. Report a failure if the section shows a competing stream's announcements.

Cover all three stream combinations: JEE, NEET, and Class 6-10.
