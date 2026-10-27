# Sewa Setu assistant: design note

**Goal (Director's note):** any AI assistant can help a citizen apply for the pension and follow it up, through an official channel, on open standards, with the portal as system of record; officers get an assistant too. An assistant must never obtain a pension the counter would not grant, and applications must be visible in both places.

## The three decisions that mattered most

1. **The rules stay in the portal; the MCP servers are only an interface.** The servers hold no business logic and no database access. They verify the token and call an internal portal API. An application can only be created by `submit_application()`, the same function the web form now uses: validation, "one live application per mobile", insert, audit, in one transaction under a per-mobile lock. So the assistant cannot admit what the counter would refuse, a double-submit cannot make two rows, and what one channel creates the other sees (same table, same status page). The audit trail records "submitted via AI assistant". The assistant may *help* (accepting "12 March 1955", "female", "sonari"); it never relaxes a rule.
2. **Identity, separation and shared phones.** Two servers, two endpoints, two token audiences: a citizen's assistant is never shown officer tools, and a token for one server is useless at the other (checked: iss, aud = that server's URL, role, scope). The caller is taken only from the token's `sub`; no tool accepts a mobile number. The assistant never sees an OTP or password: sign-in and consent happen on the portal. Sessions end absolutely (2h/8h); `sign_out` revokes the sign-in and deletes any half-saved draft; the next person on the phone sees nothing. Officer decisions need a person signed in (not an unattended program token), a reason, and are recorded as that officer. Approving an application that was under age when filed, or duplicates a live one, is blocked unless the officer has seen the flag and says so.
3. **Built for a small model with few turns, and for people who can't use the form.** Seven flat, forgiving tools. `save_details` takes whatever was just learned and returns what is saved, what is still needed and which portal rules failed, so a whole form takes a handful of calls. The age-proof photo, which an assistant cannot handle, goes by a single-use 30-minute link to a one-button upload page. Server instructions carry the manner: one question at a time, simple words, never guess facts, never fudge a date, sign out at the end.

## The one I would make differently

I would run the LLM-played personas many times each, against several small models, and let the failures rewrite the tool descriptions and instructions before anything else. So far only the scripted evals (real OAuth, real MCP, real database checks) have been run; `run_llm.py` is written but has not been run, so the conversational behaviour is the least-tested part.

## Things the department should decide (not changed by me)
- Officers all share one `admin` login, so "recorded against her name" is only as good as the login; per-officer accounts and block assignment are needed for real accountability.
- The rules allow blank gender, marital status and IFSC. The assistant asks for all three, but a pension with no IFSC cannot be paid.
- Citizens' status-portal password is their date of birth.
- One sentence of the Director's note (about limiting the agent to "a subset of blocks") was unclear in the transcript I worked from; I did not build block restrictions.
