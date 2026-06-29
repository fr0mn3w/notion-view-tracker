# Handoff: Weekly Report database (for the Notion agent)

Paste the block below to the Notion AI agent. It sets up the database and the AI summary
property. Our pipeline will then write the number columns each week; Notion AI fills the
summary. After it is created, send the database ID back so the pipeline can target it.

---

Please create a new Notion database named **Weekly Report** with exactly these properties
(names and types matter; our automation writes to them by name):

| Property | Type | Notes |
|---|---|---|
| Name | Title | Unique label per week, e.g. "Week of 2026-06-22" |
| Week Start | Date | The Sunday of the week |
| Week End | Date | The Saturday of the week |
| Views: New Systems (X) | Number | |
| Views: Other Stuff (X) | Number | |
| Views: Other Stuff (YouTube) | Number | |
| Total Views | Number | Sum of the three |
| Net Followers: New Systems (X) | Number | Followers gained that week |
| Net Followers: Other Stuff (X) | Number | |
| Net Subscribers: Other Stuff (YouTube) | Number | |
| Summary | AI autofill (custom) | See prompt below |
| Generated | Date | When the row was produced |

For the **Summary** property, use an AI autofill / custom AI property with this prompt:

> Write a 2 to 4 sentence summary of this week's channel performance using the view and
> follower columns on this row. Call out which channels grew, any standout numbers, and
> the week-over-week movement. Keep it factual and concise. Do not invent anything beyond
> the numbers shown.

If possible, set the Summary property to auto-update when the row's other properties
change, so it regenerates after the pipeline writes the weekly numbers.

Finally, share this database with the existing integration that already has access to the
Daily Snapshots / Analytics database (the same connection), and send back the database ID.

---

## Notes for us (not for the Notion agent)
- The per-channel columns map to Daily Snapshots (platform, account) pairs in config:
  - Views/Net Followers "New Systems (X)" -> (X, newsystems_)
  - "Other Stuff (X)" -> (X, the current X handle for that account)
  - "Other Stuff (YouTube)" -> (YouTube, otherstuff)
- If the Summary auto-update does not fire reliably on API writes, fall back to a templated
  summary written by the pipeline (deterministic, no AI), per the PRD.
- The pipeline uses candidate-name resolution, so minor renames are tolerated, but keep the
  names close to the above.
