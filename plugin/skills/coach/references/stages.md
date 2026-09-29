# Stages

A Company's Stage is exactly one of these values (the tools reject anything else):

| Stage | Means | Typical focus |
|---|---|---|
| `pre-idea` | wants to start, no idea chosen | cofounder, idea generation |
| `idea` | idea chosen, nothing built | talking to users, problem validation |
| `mvp` | building or just launched the first version | launch, first users, iterate fast |
| `pmf` | searching for or reaching product-market fit | retention, one segment, sales motion |
| `growth` | fit found, growing | channels, metrics, first hires |
| `fundraising` | actively raising | investor pipeline, pitch, terms |
| `scaling` | growing the organisation | hiring, management, process |
| `exit` | acquisition or later | M&A, secondary |

Pass the Founder's Stage to `coach_search` as `stage`; it boosts advice for that Stage without hiding the rest.
