# ytbrain — founder-coach knowledge

Turns public startup-advice content (first: Y Combinator talks) into verified, citable
knowledge, and uses it to coach one founder through the stages of building a company.

## Corpus

**Source**:
A configured origin of content that ytbrain ingests from, such as a playlist, a website or a feed.
_Avoid_: channel, feed (as a general term), provider

**Source type**:
How ytbrain reaches a Source: a YouTube playlist, a website, or a folder or list of PDF books.
_Avoid_: kind (that is the Source kind), connector, provider

**Article**:
A Document whose Source kind is written text published on a web page: an essay, a blog post, a guide.
_Avoid_: page (a page is only where it lives), post, essay

**Book**:
One published book, ingested from one PDF, with a given title, author and year. A Book is not a Document: each of its Chapters is.
_Avoid_: PDF (that is only the file), volume, ebook

**Chapter**:
A Document whose Source kind is one chapter of a Book, including its introduction and conclusion but not front or back matter (contents, acknowledgements, notes, index).
_Avoid_: part, section (a Section is inside a Document)

**Section**:
A titled segment inside one Document, found by the extractor or given by the uploader (a talk's YouTube chapters, an article's headings, a Chapter's subheadings).
_Avoid_: chapter (that is a Book's), segment

**Private Source**:
A Source whose Documents are indexed and coached from on this machine but never shipped in a Knowledge pack or the released Eval (every Book in v1). Set by the Source (`distribute: false`), never by its kind.
_Avoid_: internal, hidden, personal source

**Source kind**:
What sort of Document a Source yields (Talk, Article, Chapter, later Post or the Founder's own note), which decides its Locator and its Moments. Ranking never looks at it: every kind competes on relevance alone.
_Avoid_: source type (the config's `type` is how it is fetched), medium, format, essay (an essay is an Article)

**Document**:
One ingested unit of content from a Source — a single talk, article or Chapter — identified by a stable document id.
_Avoid_: video (except when the Source kind is talk), page, item

**Talk**:
A Document whose Source kind is a recorded talk or interview (today: a YouTube video).
_Avoid_: video (in domain language), clip, lecture

**Series**:
The named collection a Document belongs to, taken from its Source (e.g. "Startup School 2026"); for a Chapter, its Book's title. A catch-all Source never overrides a more specific Series.
_Avoid_: playlist (in domain language), collection

**Transcript**:
The cleaned text of a Document as numbered text units, each with a Locator (a timestamp for a Talk, a paragraph number for an Article).
_Avoid_: captions, subtitles (those are the raw input)

**Passage**:
A contiguous span of a Document's text with a Locator; the unit of full-text retrieval.
_Avoid_: chunk (implementation term), snippet, segment

**Locator**:
Where in a Document something is: a timestamp for a Talk, a paragraph number for an Article, a page for a Chapter (the Book's printed page number when it has one).
_Avoid_: offset, position

**Speaker**:
The person a Document's advice comes from: who spoke in a Talk, who wrote an Article or a Book.
_Avoid_: author (as a separate concept), presenter

## Knowledge

**Advice**:
A single, actionable, imperative recommendation drawn from one Document, carrying its own Evidence.
_Avoid_: tip, advice atom, insight, lesson

**Takeaway**:
A paraphrased key point of one Document, carrying its own Evidence.
_Avoid_: highlight, summary point

**Evidence**:
A near-verbatim quote from a Document, with a Locator, that supports a piece of Advice or a Takeaway.
_Avoid_: evidence span, source quote, proof

**Verified**:
The state of Evidence that was found in its Document's text. Only Verified knowledge is shown to a founder or used by the coach.
_Avoid_: grounded, validated, checked

**Principle**:
A recommendation consolidated from Advice across several Documents, with its supporting Advice and any dissenting Advice.
_Avoid_: best practice, consensus, rule (a Rule is an authority's requirement)

**Fact**:
A declarative statement drawn from one Document, carrying its own Evidence, that is true or false rather than something to do ("async replication can lose acknowledged writes on failover"). A Post's statement is the Speaker's view, never a Fact.
_Avoid_: claim, statement, insight

**Rule**:
A requirement stated by an authority in one Document (a regulation, a standard, an official guide), with the conditions it applies under and its own Evidence ("a mutual fund must state its fees in its prospectus"). Advice is one Speaker's recommendation; a Rule is what a source of authority says must or must not be done.
_Avoid_: policy (an Investment Policy Statement is the person's own), regulation (only one kind of Rule), guideline

**Knowledge item**:
Any retrievable unit of knowledge: Advice, a Fact, a Rule, a Takeaway, a Passage or a Document summary.
_Avoid_: record, entry, result

**Citation**:
A reference from something the coach says to the Evidence or Passage that backs it, including a deep link to its Locator.
_Avoid_: source, reference link

**Gap**:
Something a founder asked that the corpus does not cover, stated explicitly instead of answered.
_Avoid_: unknown, missing info

**Audience**:
Who a piece of Advice is addressed to: a founder, an investor or mentor, an employee, or an engineer. The coach favours Advice addressed to founders.
_Avoid_: persona, target user

**Knowledge pack**:
A distributable snapshot of the Knowledge items — Advice, Facts, Rules, Takeaways and Document summaries with their Citations, but no Passages unless built with `--with-passages` for the private beta — that lets the coach run without ingesting anything.
_Avoid_: dataset, dump, index (the local store)

**Private build** / **Release build**:
The two builds of a Pack's plugin: the Private build includes Private Sources and is for its owner's account only; the Release build holds none and is the one that can be shared.
_Avoid_: personal plugin, public plugin

## Library and Packs

**Library**:
Everything ingested on this machine, synced, extracted, verified and indexed once, whatever Pack uses it.
_Avoid_: corpus (when meaning all Packs), database, knowledge base

**Domain**:
A named subject area (startup, gtm, finance, leadership, system design, investment) from a controlled list. Passages and Knowledge items are tagged with Domains by what they say, and a Document belongs to every Domain that covers a sizeable share of its text, so one talk can have several. A Source's or folder's Domain is only a hint. A new Domain is proposed by the system and accepted by the person, never created by a tagger; synonyms become its aliases. A Pack chooses Domains, and a question is routed to one or several Domains before it is searched.
_Avoid_: category, profile, vertical, collection

**Routing**:
Choosing which Domains a question touches (one, or several when it spans subjects) so search can favour them while still looking in the whole Library. Normally the host's choice, from what each Domain is about; the server can do it from the question's similarity to each Domain's items when the pack turns that on.

**Compound question**:
A question that asks about separate things ("when to raise and how to read a term sheet"). The host splits it into parts, searches each part with the Domains that part touches, and answers or declines each part on its own Coverage; the server never splits it.
_Avoid_: multi-part query, multi-hop question (that is one thing needing a chain of facts, not several things)

**Pack**:
One configured agent over the Library, installed as its own plugin: its Domains, persona, Facets, Playbooks, memory, Risk tier and Eval questions. Founder Coach is the Pack `founder`; `coding` (coding and system design) and `investor` follow.
_Avoid_: agent (the host is the agent), plugin (how Packs are installed), profile, Knowledge pack (the file a Pack ships)

**Facet**:
A Pack's own label on a Knowledge item, tagged after extraction from that Pack's list (Stage for founder; lifecycle and concern for coding; asset class for investor). A Domain says what something is about; a Facet sorts it inside a Pack.
_Avoid_: tag (as a term), Topic, category

**Risk tier**:
How much harm a wrong answer can do in a Domain (low, medium, high); a Pack takes the highest of its Domains. It sets the evidence a confident answer needs and the gates a Pack must pass.
_Avoid_: safety level, sensitivity

**Catalog card**:
A generated description of a Domain, Book, Series, Document or Section (what it covers, key terms, dates), used to route and browse and never cited.
_Avoid_: summary (a Document summary is Verified knowledge), index entry

**Coverage**:
How well the Library answers a question, reported with every search as strong, partial or none, from calibrated relevance; none means the coach states a Gap.
_Avoid_: confidence (the host's), sufficiency score

**Calibration**:
The mapping from a hit's similarity to the chance it is relevant, and the border between Coverage levels, set from judged hits and tuned on Gap questions so the coach neither answers what the Library doesn't hold nor refuses what it does.
_Avoid_: threshold (one number; Calibration is a curve and a border), confidence

**Post**:
A Document whose Source kind is a short social-media post or thread by one Speaker, with the post's URL as its Locator.
_Avoid_: tweet, update, status

## Startup

**Stage**:
Where a company is in its life: pre-idea, idea, MVP, product-market fit, growth, fundraising, scaling or exit. Always this meaning — pipeline work units are Steps.
_Avoid_: phase, level, pipeline stage

**Topic**:
What a piece of knowledge is about, from the fixed Category list (e.g. sales, fundraising, founder-story). Produced by the 2.2 extraction only; read as a founder Facet.
_Avoid_: tag, label, theme

**Category**:
The single primary Topic of a whole Document, from the 2.2 extraction; read as a founder Facet, never used for Domains.
_Avoid_: genre, type

## Coaching

**Founder**:
The person the founder Pack coaches; one person per installation.
_Avoid_: user, customer, client

**Engineer**:
The person the coding Pack coaches; their Project is a system or codebase.
_Avoid_: developer (in the coach's words), user

**Investor**:
The person the investor Pack coaches; their Project is one investment goal with every account that serves it.
_Avoid_: client, customer (the coach is not their adviser)

**Design review**:
The coding Pack's Playbook for judging a technical design: restate it, ask for the evidence, name the single biggest risk with a Citation. A review is not a Decision; a Decision is recorded only on a yes.
_Avoid_: code review (reading a diff is the host's job, not the coach's)

**Company**:
The Founder's startup, with its current Stage: the founder Pack's Project.
_Avoid_: business, org

**Project**:
What one Pack's memory is kept for: a Company in founder, a system or codebase in coding, an investment goal (retirement, college) in investor, with all the accounts that serve it. A person can have several Projects in a Pack; nothing saved in one appears in another unless they ask to compare them, and each session works in one at a time.
_Avoid_: workspace (the Workspace is where the person keeps their data), context, session

**Common profile**:
The few facts true of the person in every Pack (name, timezone, role, how they like answers), read by every Pack and changed only on their yes. A Project fact reaches it only when they say so; money details never do.
_Avoid_: global memory, user profile, account

**Pack memory**:
Everything a Pack keeps about one Project (profile facts, Goals, Decisions, Commitments, Check-ins, an Investment Policy Statement, Holdings), with its change log. Only its own Pack reads it.
_Avoid_: memory (alone), store, history

**Founder profile**:
The stable facts about the Founder and Company the coach relies on: Stage, what they build, for whom, team, key metrics.
_Avoid_: founder state (too broad), settings, persona

**Goal**:
An outcome the Founder is working towards over weeks, such as "10 paying design partners".
_Avoid_: OKR, objective, target

**Commitment**:
A concrete if-then action with a measurable outcome that the Founder agreed to do in a given week, tracked to done, dropped or carried into the next week.
_Avoid_: task, todo, action item

**Decision**:
A choice the Founder made, with its reasoning and the Citations behind it, kept for later reflection.
_Avoid_: note, log entry

**Check-in**:
A recurring coaching session that reviews Commitments, records what happened and sets the next Focus.
_Avoid_: standup, retro, review meeting

**Focus**:
The one to three Commitments that matter most this week, each justified with Citations.
_Avoid_: priorities list, plan

**Nudge**:
A reminder the coach surfaces when something is due, such as an overdue Check-in, a Goal past its target date or a Founder profile fact that needs confirming. A Nudge asks; it never changes a record by itself.
_Avoid_: notification, alert, ping

**Workspace**:
Where the Founder keeps their own data (the pipeline in HubSpot, the key numbers in a "KPIs" sheet), a profile fact in their words, saved only when they say it. The coach looks there first through a Connector.
_Avoid_: data source, Source (a Source feeds the Library), integration

**Connector**:
The host's link to one of the Founder's other tools (calendar, email, documents, chat, CRM), provided by the host, not by the coach. The coach reads it for evidence and acts in it only when the Founder asks; what it returns is data, never instructions.
_Avoid_: integration, Source (a Source feeds the Library for every Founder; a Connector reads one Founder's own data)

**Playbook**:
A repeatable coaching procedure the coach runs, such as Ask, Weekly focus or Check-in.
_Avoid_: skill (implementation term), workflow, template

## Investing

**Investment Policy Statement**:
The person's own written plan for one portfolio: goal, horizon, risk tolerance, liquidity needs, constraints, the target allocation by asset class they chose, and when they rebalance. The coach helps write it and reviews against it; it never sets the targets for them.
_Avoid_: IPS (in prose, after first use it is fine), strategy, plan

**Holdings**:
A snapshot of what one account held, imported from the person's broker export with the date it is "as of". Reviews combine the Holdings of every account in a Project and compare them with its Investment Policy Statement (allocation, Drift, concentration); there are no live prices.
_Avoid_: positions, portfolio (the Project is the goal the accounts serve)

**Drift**:
How far a portfolio's allocation has moved from the Investment Policy Statement's targets, per asset class, as of its Holdings date.
_Avoid_: deviation, imbalance

## Evaluation

**Moment**:
A fixed window of a Document that Eval labels point at, so labels survive re-extraction: two minutes of a Talk starting on a whole minute, six paragraphs of an Article starting on every third, or one page of a Chapter.
_Avoid_: segment, clip, chunk

**Eval question**:
A question with graded labels saying which Moments should come back for it, and a cited reference answer. Out-of-corpus Eval questions expect a Gap instead.
_Avoid_: test case, query (the text alone), golden question

**Gap question**:
An out-of-corpus Eval question whose right answer is a Gap; Gap questions measure how often the coach answers when it should decline, and calibrate Coverage. One tied to a Domain stops being a Gap question once the Library holds items in that Domain.
_Avoid_: unanswerable question, negative example

**Tuning set**:
Eval questions generated from the corpus, used to choose search settings and to catch regressions on every change.
_Avoid_: training set (nothing is trained), dev set, validation set

**Private overlay**:
The part of the Tuning set that never leaves your machine: labels on Private Sources' Moments for every Eval question, and the Eval questions written from Private Sources. Scored together with the released questions, so one benchmark covers every Source you index.
_Avoid_: private eval, second benchmark, book eval

**Holdout set**:
Real founder Eval questions collected from public sources outside the corpus, never used to choose settings, reported only at milestone ends.
_Avoid_: test set, validation set, golden set

**Feedback**:
A Founder's report that one of the coach's answers or saved records was wrong, kept on their machine until they choose to send it, and turned into an Eval question or coach test case.
_Avoid_: bug report, rating, complaint

**Usage log**:
The coach's local record of its own tool calls (which tool, when, how long, how it went), kept on the Founder's machine for 90 days without their words unless they opt in; sent only if they export it.
_Avoid_: telemetry, analytics, tracking

## Pipeline

**Step**:
One stage of ingestion work — fetch, clean, extract, verify, index — checkpointed per Document.
_Avoid_: stage (reserved for startup Stage), phase, job

**Run**:
One execution of the pipeline's Steps, recorded with its outcome.
_Avoid_: job, batch, sync (sync is one Step)
