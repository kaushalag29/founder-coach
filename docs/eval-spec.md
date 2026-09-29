# ytbrain eval benchmark — specification v1

Status: **accepted 2026-09-23**, checked against 2025-2026 practice (BEIR, MTEB v2, TREC RAG 2024/25,
TREC Podcasts, CRAG, BRIGHT, CourseTimeQA, UMBRELA; sources at the end). Glossary: [CONTEXT.md](../CONTEXT.md).
Built with **no manual question writing or labelling**; where the standards expect a human check,
the benchmark says honestly that it has not had one (§8).

## 1. Units: Moments, not Knowledge items

Knowledge items change on every re-extraction, so labels never point at them. Ground truth is a
**Moment**: a fixed two-minute window of a Talk. This is the TREC Podcasts convention (2-minute
segments starting every minute, so they overlap by 60 s). An Article's Moment is a run of six
paragraphs starting every third paragraph (about a two-minute read, the same 50 % overlap).

- Moment id: talks `{youtube_id}_{start_s:05d}`, e.g. `dQw4w9WgXcQ_00120`; articles
  `{doc_id}_p{start_paragraph:05d}`. Ids may contain `_`, so the start is parsed after the last `_`.
- A Talk has Moments at 0, 60, 120 s, … up to its duration.
- **Mapping a system's results to Moments (for scoring).** An Advice, Takeaway or Passage result
  maps to the Moment whose start is the largest multiple of 60 s at or below its Locator. A repeat
  of an already-listed Moment is removed, so the run lists each Moment once, in rank order.
- **Document summaries** carry no Locator. They are scored only at talk level (§6).
- Any system that splits the Talks its own way can map to these ids.

## 2. Splits

| Split | BEIR name | Size | Origin | Use |
|---|---|---|---|---|
| Tuning set | `dev` | 150 | Generated from our corpus | Choosing settings; every change |
| Holdout set | `test` | ≤ 50 | Real founder questions: a pre-2024 CC BY-SA Startups Stack Exchange dump (archive.org; the current dumps need a login and forbid LLM training); YC Office Hours later | Milestone ends only; never used to choose anything |

## 3. Files (BEIR layout + TREC copies + canonical spans)

```
eval/
  README.md            data card: Hugging Face YAML + datasheet sections (§8)
  croissant.json       Croissant 1.1 core + RAI fields
  queries.jsonl        both splits, schema §4
  answers.jsonl        reference answers, nuggets, citations (§5)
  moments.jsonl        canonical graded spans: the ground truth labels
  corpus.jsonl         Moment manifest: _id, title, url, speaker, published_at, plus youtube_id, start_s,
                       end_s (talks) or doc_id, start_paragraph, end_paragraph (articles); no text
  qrels/dev.tsv        query-id \t corpus-id \t score   (header row, integer 0-3)
  qrels/test.tsv
  trec/dev.qrels       qid 0 moment_id grade   (for ranx / trec_eval)
  trec/test.qrels
  CHECKSUMS            sha256 of every file
```

Written today (v1.3.0): `queries.jsonl`, `moments.jsonl`, `corpus.jsonl`, `qrels/dev.tsv`,
`trec/dev.qrels`, `VERSION`, `CHECKSUMS`. The test split, `answers.jsonl`, the data card,
`croissant.json` and `eval hydrate` are planned (phase3-plan §0, optional).

- **No transcript text is shipped.** Captions are the uploader's content (README, Responsible use).
  `corpus.jsonl` is a manifest; `ytbrain eval hydrate` rebuilds text locally from the user's own
  transcripts. This makes the benchmark BEIR-shaped but not an MTEB task, because MTEB embeds corpus
  text. That is deliberate; reconsider only if a licensable text form exists.
- **qrels are derived, not hand-kept.** A labelled span gives its grade to every Moment it overlaps;
  a Moment hit by several spans keeps the highest. Grade-0 judgments are included, so Judged@10 can
  be reported.

## 4. `queries.jsonl` record

| field | type | meaning |
|---|---|---|
| `_id` | str | `dev-0001` / `test-0001`; never reused |
| `text` | str | the question as a founder would ask it |
| `split` | `dev`\|`test` | |
| `question_type` | enum | see taxonomy below |
| `stage` | enum[] | our Stages: pre-idea, idea, mvp, pmf, growth, fundraising, scaling, exit |
| `topic` | enum[] | our 17 Categories |
| `temporal` | `static`\|`slow_changing`\|`time_sensitive` | e.g. YC deal terms are time-sensitive |
| `valid_as_of` | date | corpus freeze date |
| `answerable` | bool | false = out-of-corpus: the right behaviour is a Gap |
| `popularity` | `head`\|`torso`\|`tail` | from the number of Moments graded ≥ 2 (≥ 10 / 3-9 / ≤ 2) |
| `origin` | `synthetic`\|`yc_office_hours`\|`stackexchange` | |
| `source_url` | str | the question's citation: seed Evidence deep link, or the original post |
| `source_author`, `source_author_url` | str\|null | required for Stack Exchange attribution |
| `license` | str | per record: `CC-BY-SA-4.0` for Stack Exchange (the version depends on post date), `CC-BY-SA-4.0` for ours |
| `creation` | object | `generator_model`, `prompt_id`, `seed_moment` (synthetic), `lexical_overlap`, `embed_sim`, `bm25_source_rank` |
| `human_verified` | bool | false in v1 (§8) |

**Taxonomy (`question_type`):**
- `how_to_advice`: "how do I …"
- `conditional_stage`: advice given a situation or Stage
- `single_fact`: what a Talk or speaker said
- `multi_source_synthesis`
- `comparison_disagreement`
- `time_sensitive`: YC process and terms
- `false_premise`
- `out_of_corpus`

Generated questions cover mostly the first three; real questions are classified by the LLM.

## 5. `answers.jsonl` record (for answer-level scoring in M3)

| field | type | meaning |
|---|---|---|
| `qid` | str | |
| `expected_behavior` | `answer`\|`abstain` | `abstain` when `answerable` is false (a Gap) |
| `reference_answer` | str | ≤ 250 words, written only from Moments graded ≥ 2, every sentence cited |
| `nuggets` | list | `{text, importance: vital\|okay, moments: [moment_id]}`, TREC RAG AutoNuggetizer style |
| `citations` | list | `{moment_id, youtube_id, start_ms, url, title, speaker, quote}`. `url` deep-links to the second; `quote` ≤ 40 words, attributed |

M3 scores the coach's answers with nugget coverage (V_strict), sentence-level citation support, and
CRAG-style truthfulness: +1 correct, 0 Gap, −1 wrong.

## 6. How labels are made (all automatic)

1. **Questions.**
   - **Tuning set:** sample 150 Verified Advice and Takeaways.
     - Stratify by Category, Stage, year and caption kind.
     - At most 1 per Talk and 3 per speaker.
     - The generator writes a question from the item text, never its quote.
     - A question is rejected when:
       - its content-word overlap with item or quote is > 0.5;
       - its bge-m3 similarity to the quote is > 0.85;
       - it is a near-duplicate of another question (similarity > 0.9).
     - `bm25_source_rank` is recorded to report how "easy" the set is.
   - **Holdout set:** collect questions from the two sources.
     - An LLM keeps only questions an early-stage founder would ask a coach.
     - Near-duplicates are removed.
     - Balance across Stages and Topics; cap 50.
2. **Pooling.** The union of the top-k from five variants: full-text only, vector only, hybrid,
   hybrid + rerank, and hybrid on an LLM rewrite. k = 20 for the Tuning set and k = 50 for the
   Holdout set. The seed Moment is always included. Results map to Moments (§1).
3. **Grading.** The UMBRELA 0-3 prompt:
   - 0: unrelated
   - 1: related, doesn't answer
   - 2: partly answers or buried
   - 3: dedicated, exact answer

   Grading is done by two judges from model families different from the generator (generator:
   DeepSeek; judges picked by `ops/probe_models.py`, e.g. Gemma and Qwen). If they differ by ≥ 2, a
   third judge adjudicates and the median is kept; otherwise the lower grade is kept (also when no
   third judge is available). Nothing is
   silently dropped. No system under test uses an LLM reranker, which avoids the known
   self-preference bias.
4. **Self-check.** A generated question whose seed Moment is graded < 2 by the judges is discarded as
   a bad question. The rate is reported as judge recall on seeds.
5. **Out-of-corpus.** A Holdout question with no Moment graded ≥ 2 in the depth-50 pool is marked
   `answerable: false`. It is excluded from nDCG and scored on abstention.
6. **Answers.** A reference answer and its nuggets are written from the Moments graded ≥ 2. A
   judge checks each sentence against its citation; unsupported sentences are removed.
6. **Pool extension for later systems.** The pool comes from the systems that existed when
   the split was built. A new configuration (e.g. the Knowledge pack's smaller models)
   retrieves Moments no judge has seen, and those count as irrelevant, so it looks worse
   than it is. The first pack run had 43% of its top 10 unjudged. `ytbrain eval judge
   --config <c>` grades a saved run's unjudged top-10 Moments with the same judges and
   prompt, and re-releases the labels as a new minor version (1.0.0 -> 1.1.0). This is
   standard TREC practice. Every run records the labels it was scored against
   (`qrels_sha256`), so a baseline scored against older labels is flagged, never silently
   compared.

## 7. Metrics and reporting (`ytbrain eval run`)

- **Primary:** nDCG@10 (ranx, linear gain, trec_eval-compatible) at Moment level.
- **Also:** Recall@10 and @50, MRR@10, Judged@10, strict variants counting only grade ≥ 2
  (`-l2`), talk-level nDCG@10 (summaries count here), and abstention precision and recall with the
  Gap threshold.
- **Uncertainty:**
  - 95% bootstrap confidence intervals over queries (10 000 resamples).
  - When comparing configurations: the paired difference with its 95% bootstrap interval,
    plus a paired randomization p-value. nDCG@10, decided in advance as the primary metric,
    is tested on its own; the secondary metrics are Holm-corrected among themselves.
  - 150 questions resolve differences of roughly 0.02-0.04 nDCG@10 (observed on dev: the
    reranker's +0.017 has the interval [-0.004, +0.038]).
- **Ceilings:** a question with more than k relevant Moments can't reach Recall@k = 1, so
  each Recall@k is printed next to its best possible value (dev: Recall@10 0.342, strict
  0.749, Recall@50 0.999).
- **Breakdowns:** by `question_type`, `origin`, `popularity`, `stage`, `topic`; groups under
  10 questions are marked as too small to compare.
- **When a comparison counts:** only when both runs were scored against the same labels
  and at least 90% of the new run's top-10 Moments are judged. Otherwise the verdict is
  *inconclusive* (exit code 3), with the command that fixes it; a failed gate is exit code 1.
  `ndcg@10-cond` (nDCG over the judged part of the ranking) is reported as a pool-bias check:
  with incomplete judgments, the true nDCG@10 lies between it and `ndcg@10`.
- **Reach:** each run reports the share of relevant Moments the configuration can return at
  all. A store without Passages can't return a Moment that has no Advice or Takeaway; the
  Knowledge pack reaches 74.2 % of the dev set's relevant Moments (labels v1.3.0).
- **Re-scoring:** `ytbrain eval rescore --config <c>` scores a saved run against the current
  labels in seconds, without searching again.
- **Run files:** every `eval run` also writes its ranked Moments in TREC run format
  (`data/eval/runs/<split>-<config>-<time>.run`), so trec_eval or ranx can re-score it.
- **Label quality:**
  - weighted Cohen's κ between the judges
  - adjudication rate
  - judge recall on seeds
  - share of synthetic questions where full-text search ranks the seed first
- **Gate:** a change that lowers dev nDCG@10 or Recall@10 by > 0.05 fails; pack configurations
  instead fail when nDCG@10 is more than 0.03 below `full` (ADR-0009).
- **Configurations:**
  - `full` (bge-m3 + bge-reranker-v2-m3), `no-rerank`, `stage-boost`, `full-no-passages`
  - `pack` and `pack-no-rerank`: the Knowledge pack's ONNX models; `--pack PATH` and
    `--label NAME` score variants such as a `--with-passages` pack

## 8. Data card, versioning, licensing

- **`README.md`** (Hugging Face YAML + Datasheets for Datasets sections) states:
  - models, prompts and versions used to generate, pool and judge
  - pool depth and variants
  - agreement statistics
  - corpus freeze date and Talk count
  - known biases: YC-only voice, English, auto-caption errors, 2013-2026 skew
  - intended use
  - a canary string
  - **"labels are LLM-judged (silver); not yet human-validated"**
- **Human validation (optional).** A human check can later add a stratified 150-200 pair sample,
  two annotators, and report κ against the judges and human-human κ. It raises the labels to
  "gold" and changes the data card, not the file format.
- **Versioning.** Semver; the released labels are v1.3.0 (`eval/VERSION`). Bump the minor version
  when questions are added or `eval judge` extends the pool, and the major version when labels are
  regraded. `CHECKSUMS` holds a sha256 per file. Released as a git
  tag (and on the Hugging Face Hub if published), with the revision pinned.
- **Licences.**
  - Records containing Stack Exchange text are CC BY-SA with full attribution (site, post link,
    author name and profile link); ShareAlike therefore applies to the query files.
  - YC Office Hours questions are paraphrased and link to the post.
  - Quotes are ≤ 40 words with attribution and a deep link.
  - Whether the public release may include quotes is confirmed by the maintainer before
    publishing (plan §14).

## 10. Implementation (M2, decided 2026-09-23)

| Concern | Decision |
|---|---|
| Code | `ytbrain/eval/`: `moments`, `metrics`, `db`, `llm`, `generate`, `pool`, `judge`, `build`, `files`, `run`, `coach` (`coach_cases/`: the G4, G5 and G6 cases) |
| Commands | `eval build --set dev\|test [--max-cost] [--workers] [--limit]`; `eval run --set … --config full\|no-rerank\|stage-boost\|full-no-passages\|pack\|pack-no-rerank [--pack PATH] [--label NAME] [--smoke] [--save-baseline] [--compare CONFIG]`; `eval status`; `eval judge --config C [--depth 10]`; `eval rescore --config C [--save-baseline] [--compare C]`; `eval coach [--gate g2\|g4\|g5\|g6] [--model] [--max-turns] [--host claude\|openrouter] [--limit]` (docs/commands.md) |
| Resume | Every expensive result is a row in `data/eval/eval.db` (SQLite, WAL), keyed by what produced it: seed questions, generated questions, rewrites, pools (written in one transaction, then marked complete), and grades per (question, Moment, judge, prompt version). A re-run skips what exists; a changed prompt re-grades only under its new version. Released files are written atomically, and only at finalize |
| Parallelism | LLM calls run on daemon worker threads (`YTBRAIN_EVAL_WORKERS`, default 8), and results come back to the main thread, which does all writing (one SQLite writer). Ctrl+C exits immediately; unfinished work is redone. Local models (embedding, reranker) run on the main thread only |
| Backoff | Reuses the extract runner's HTTP layer: exponential backoff with jitter on 429/5xx/dropped connections/bad 200s, Retry-After honoured, a 429 pauses all workers, and one shared requests-per-minute budget (`YTBRAIN_EVAL_MAX_RPM`, default 60) |
| Failure | A failed call leaves its job undone for the next run, and never becomes a fake 0 grade. A judge answer that skips a passage fails the whole call. A refused key, model or credit (`BackendUnavailable`) stops the build cleanly |
| Spend cap | OpenRouter reports cost per call (`usage.include`). Spend is recorded per split, and the cap (`--max-cost`, default $5) is cumulative across runs, so resuming never double-spends; at the cap the build stops cleanly and says how to continue |
| Models | Preflight checks the generator and the three judges are served. A missing one is replaced by the cheapest served model of its family that accepts a JSON schema. It refuses judges from the generator's family or two judges from one family |
| Grading batches | 5 Moments per judge call, each scored on its own, order shuffled per judge. This departs from strictly pointwise UMBRELA grading to fit the call budget, and the data card states it |
| Scale | Questions, pools and grades are independent per question, so the set grows by adding seeds or sources and re-running; labels by Moment stay valid across re-extraction and new chunking |

## 9. Sources

- BEIR: https://github.com/beir-cellar/beir/wiki/Load-your-custom-dataset
- MTEB v2: https://huggingface.co/blog/isaacchung/mteb-v2 and https://docs.mteb.org/contributing/adding_a_dataset/
- ranx: https://amenra.github.io/ranx/
- TREC Podcasts segments: https://trecpodcasts.github.io/participant-instructions-2020.html
- TREC RAG: https://trec-rag.github.io/
- UMBRELA: https://arxiv.org/html/2406.06519v1
- LLM-judge limits: https://arxiv.org/html/2412.17156v2
- Nuggets and support: https://arxiv.org/html/2411.09607v1 and https://arxiv.org/html/2504.15205v1
- CRAG: https://github.com/facebookresearch/CRAG
- BRIGHT: https://huggingface.co/datasets/xlangai/BRIGHT
- CourseTimeQA: https://arxiv.org/html/2512.00360v1
- Croissant 1.1: https://docs.mlcommons.org/croissant/docs/croissant-spec-1.1.html
- NeurIPS 2026 E&D: https://neurips.cc/Conferences/2026/CallForEvaluationsDatasets
- Stack Exchange attribution: https://stackoverflow.blog/2009/06/25/attribution-required/
