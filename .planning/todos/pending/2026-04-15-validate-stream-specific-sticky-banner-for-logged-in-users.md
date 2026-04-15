---
created: 2026-04-15T18:28:01.337Z
title: Validate stream-specific sticky banner for logged-in users
area: auth
files:
  - scraper.py:482-608
  - config/urls.yaml:51-53
---

## Problem

When a user is logged in, the homepage (and potentially other pages) displays a sticky banner/CTA. This sticky should be contextual to the user's enrolled stream:

- A **JEE** logged-in user should only see a JEE-relevant sticky (not NEET or Class 6-10).
- A **NEET** logged-in user should only see a NEET-relevant sticky.
- A **Class 6-10** logged-in user should only see a Class 6-10 sticky.

There is currently no assertion in the authenticated validation pass that checks the content of the sticky banner against the expected stream. A regression could silently display the wrong stream's sticky to a logged-in user.

## Solution

In the authenticated scraping/validation pass for each auth session:
1. Locate the sticky element on the page (e.g. by a fixed/sticky CSS position selector or a known data attribute/class).
2. Extract the visible text or CTA label from the sticky.
3. Assert that the sticky content matches the expected stream — either by keyword match (e.g. "JEE" in text for a JEE session) or by confirming it is a generic/stream-agnostic sticky when that is the intended design.
4. Report a failure if the sticky is absent when one is expected, or if it advertises the wrong stream.

Cover all three stream combinations: JEE, NEET, and Class 6-10.
