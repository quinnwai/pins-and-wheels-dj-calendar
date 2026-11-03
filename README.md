# Pins & Wheels DJ Nights

A calendar feed of the guest DJs at Adult Skate Night at [Pins & Wheels](https://www.pinsandwheels.com/events) in Austin, rebuilt every day from the lineup flyer on their events page.

**Subscribe:** https://quinnwai.github.io/pins-and-wheels-dj-calendar/ has one-tap buttons for Apple and Google Calendar.
Feed URL for anything else: `https://quinnwai.github.io/pins-and-wheels-dj-calendar/pins-and-wheels-dj.ics`

## How it works

A GitHub Actions job runs [`pnw_dj.py`](pnw_dj.py) every morning. Only one step uses a model; code decides everything else.

| Step | What happens | Decided by |
|---|---|---|
| 1 | Fetch `/events` and read the "Upcoming Events" carousel from the JSON Squarespace embeds in the page (`data-current-context`): each slide's title, text, image id, and upload time. No browser. | Code |
| 2 | Skip images already handled. `data/flyers.json` caches every image's result by its Squarespace image id. | Code |
| 3 | Send each new image to Claude twice. It transcribes whether it's a DJ lineup, every date with its DJ, and the printed hours line. | Claude Sonnet 5.5, structured output |
| 4 | Both reads must match exactly, or nothing is published and the next run tries again. | Code |
| 5 | Validate and date it: at least 3 nights in one month; the year is the one nearest the upload date; every date must be a scheduled skate night, using hours parsed from the flyer or else the slide text. | Code |
| 6 | Rebuild `docs/pins-and-wheels-dj.ics` from every cached lineup. UIDs are per date, so a corrected flyer updates events instead of duplicating them. Same input, same bytes, so git only changes when the lineup does. | Code |

Choices worth knowing about:

- **Not keyed on the slide title.** It was "DJ Line Up" in 2026, but every slide was "New List Item" in October 2025. Each new image is classified instead, and the result is cached by image id.
- **Not OCR.** Apple Vision read the DJ names fine but garbled the techno date font (`3RD` became `BRO`, `14TH` became `1ЧТH`). A vision model got every date and name right across repeated runs, and the code above checks its work.
- **Daily, not monthly.** The May 2026 flyer went up on May 5, after the May 2 night. On most days a run is one page fetch and no model calls.

## When it fails

The workflow fails, and GitHub emails you, in these cases:

- **`RETRY`**: the two reads disagreed or the model stopped early. Nothing is published, and the next daily run tries again automatically.
- **`REJECTED`**: both reads agreed, but the flyer broke a rule, for example a date that isn't a skate night. That is reported once and cached. To retry, delete the image's entry in `data/flyers.json` and run the workflow.
- **No carousel slides found**: the page layout changed. The last good calendar stays up.

GitHub also disables scheduled workflows in public repos after 60 days without activity. Each new lineup adds a commit, so this only happens if they stop posting flyers. If it does, re-enable it from the Actions tab.

## One-time setup

1. **Anthropic auth via [workload identity federation](https://platform.claude.com/docs/en/manage-claude/workload-identity-federation).** No API key is stored anywhere. Each run, GitHub mints a ~5 minute OIDC token for this repo's `main` branch, and the Anthropic SDK trades it for a ≤10 minute access token, scoped to inference in one workspace.
   - In the Claude Console (org admin), open Workload identity, then **Connect workload**, then **GitHub Actions**.
   - Rule values:
     - audience: `https://api.anthropic.com`
     - `subject_prefix`: `repo:quinnwai@54592956/pins-and-wheels-dj-calendar@1444885206:ref:refs/heads/main`
     - claims: `repository_owner=quinnwai`, `ref=refs/heads/main`
     - scope: `workspace:inference`
     - token lifetime: 600 seconds
   - About the `subject_prefix`: repos created after July 15, 2026 use GitHub's immutable subject format with numeric IDs, here owner `54592956` and repo `1444885206`. `gh api repos/quinnwai/pins-and-wheels-dj-calendar --jq '"\(.owner.id) \(.id)"'` prints them. Each run's log also prints the claims the rule sees.
   - Add the outputs as repository **variables**. They're identifiers, not secrets, and the rule does the gating:
     - `ANTHROPIC_ORGANIZATION_ID` (org UUID)
     - `ANTHROPIC_SERVICE_ACCOUNT_ID` (`svac_...`)
     - `ANTHROPIC_FEDERATION_RULE_ID` (`fdrl_...`)
   - A failed exchange is an opaque 401. The reason (for example `match_subject_prefix`) is in the Console's workload identity authentication history.
2. **Pages:** Settings, Pages, Source: **GitHub Actions**.
3. **First run:** Actions, Update DJ calendar, Run workflow.

## Local development

```bash
uv sync
uv run pytest
```

The tests run the deterministic pipeline against a saved copy of the real page, with a fake transcription standing in for the model.

## Cost

The first run reads all ~6 carousel images twice, about $0.15 on Sonnet 5.5. After that, a new flyer costs a couple of cents. GitHub Actions and Pages are free for public repos.
