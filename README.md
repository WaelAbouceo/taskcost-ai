# TaskCost AI

**The Enterprise AI Cost & Performance Benchmark.** Compare AI models by what they accomplish, not by what their tokens cost.

*Powered by SovereignEG.*

Token prices don't tell a buyer what a model costs. A cheap model that fails half the time, needs retries or writes long reasoning traces can cost more per finished job than an expensive one. TaskCost AI gives models realistic enterprise tasks in natural language, has a panel of judge models grade every answer against plain-language criteria, and reports:

> **Cost per successful task** = total spend on the model (every attempt, including failures and retries) ÷ number of tasks completed successfully

Quality, reliability and latency are reported next to it, so cost is never read alone.

## Web interface

```bash
uv sync
uv run taskcost ui            # then open http://127.0.0.1:8000
```

1. **New job**: describe the work and upload documents (.txt, .md, .pdf). The design agent breaks it down (about a minute).
2. **Plans**: check each simple task's expected answer and checks, edit them, mark the plan as checked, pick models and run. Test mode runs with simulated models at no cost.
3. **Results**: the cost of the whole job per model, the recommended best model for each simple task, and the most common failures. Click any cell to see that model's answer and each reviewer's verdict.

The interface is a small FastAPI server (`taskcost/server.py`) and one plain HTML/CSS/JavaScript page (`taskcost/web/`), with no build step.

## Sovereignty: where your data goes

The home page detects the user's country from the browser (time zone, then language; nothing is sent to a
lookup service) and offers three levels:

- **Open**: location doesn't matter; the cheapest correct model wins.
- **Prefer local**: models hosted in the user's country first, then places their law allows, then their
  region, then anywhere. Every fallback is explained on the result.
- **Strict**: only the user's country or places their law allows, for the compared models, the helper models
  that read the request, and the reviewers. Models outside are skipped before anything is sent; if nothing
  qualifies, the request is refused.

Countries, their data laws and what Strict accepts are in `knowledge/jurisdictions.yaml` (a simplification,
not legal advice). Logic: `taskcost/sovereignty.py`.

**The hosting agent** (`taskcost/hosting.py`) decides where each model runs, daily while the server is up
(`hosting_refresh_hours`) or now with `uv run taskcost hosting`:

1. SovereignEG's model list: `source: sovereign` models run on its own GPUs in Egypt; every other model's
   `engines` name the providers SovereignEG may route it to.
2. Each provider's official pages (`knowledge/hosting_sources.yaml`) are fetched, and a model hosted in Egypt
   reads the passages about locations. It must quote the page for every location.
3. A location is kept only if its quote appears word for word on the page and names the place. Otherwise the
   last verified record is kept, or the provider is marked unverified. Unchanged pages are not re-read.

Results are in the app under **Where models run**, with every quote and its source link. Providers that forward
requests to companies they don't name (OpenRouter) make a model's location unverified, which Strict treats as
outside.

## Model variants and chatbot speed

`model_variants` in `benchmark.yaml` defines the same model with other settings, named `model-id@name`, for
example `SovereignEG/Qwen3.8-27B-FP8@instant` (no reasoning). A variant is measured, ranked and recommended like
any model, and has its base model's licence and location. On the 10 enterprise tasks the `@instant` version of
the Egypt-hosted Qwen passed 20 of 20 at about a third of the default's time and cost.

Chatbot speed is judged on chatbot-length answers (up to `SHORT_ANSWER_TOKENS`, 600 tokens) once a model has
at least `MIN_SHORT_RUNS` (8) of them measured. A long report is slow from any model, and a chatbot rarely
writes one. Otherwise, and for other use cases, every answer counts (`taskcost/registry.py`).

## Insights: the log reviewer agent

The server keeps a usage log (`.cache/usage/events-YYYY-MM.jsonl`, never committed): requests, finished
comparisons and plans, use cases the user corrected, Strict refusals, measurements, own examples and failed
jobs. It stores choices, outcomes, costs and durations, never the user's request text (only its length and
the short title of the concrete task). `taskcost/usage.py`.

The log reviewer (`taskcost/reviewer.py`) runs daily (`review_refresh_hours`), from the **Insights** page, or
with `uv run taskcost review`:

1. Code computes the usage facts for the last `review_days` days, such as Agent 1's hit rate, how often a
   guessed use case is corrected, fallbacks, each model's pass, error and too-slow rates per use case, cost
   split and waiting times. It also flags signals: facts past a threshold that usually call for a change.
2. A model hosted in Egypt (`review_model`) writes up to six recommendations, with what each fact means and
   the product's limits (for example the chat speed limit) given alongside.
3. A recommendation is dropped if it cites a fact that doesn't exist or states a number the facts don't
   support. The page shows the cited facts' real values next to each one.

On first use, past usage is rebuilt from the runs already saved in `results/`.

## How it works: from real work to simple tasks

The **simple task** is the unit of economics: one clear job with one answer, done in one AI call and checkable on its own.

1. **Input**: a project, a complex task or a simple task, plus documents. Example: "Extract the key data from this contract".
2. **Design agent** (`taskcost plan`): a strong model breaks the work down into simple tasks, with dependencies, an expected answer and acceptance criteria for each. The plan is saved as YAML so a person can review and edit it.
3. **Worker models** (`taskcost execute`): every model under test does every simple task, in dependency order, several times. Results of earlier tasks are passed to later ones; if a task fails, tasks that need it are blocked.
4. **Judge panel**: checks each simple task against the source documents and its criteria.
5. **Cost ledger**: cost per correct simple task, rolled up to complex tasks and the whole job, plus the **recommended mix**: the cheapest reliable model for each simple task.

```bash
uv run taskcost plan "Extract the key data from this contract" \
    --doc contract=examples/contracts/msa-delta-nilecloud.txt --out plans/contract-extraction.yaml
uv run taskcost show plans/contract-extraction.yaml      # review; edit the file, then set reviewed: true
uv run taskcost execute plans/contract-extraction.yaml   # add --mock to try it offline
```

Check the design agent's expected answers before trusting results: they are what the judges compare against. Documents can be `.txt`, `.md` or `.pdf`.

## Standard task suite

`tasks/` holds a fixed, hand-written suite of simple tasks with calibration counterexamples, used to compare models on a common baseline.

## Quick start

```bash
uv sync
uv run taskcost validate          # offline check that every task is well formed
uv run taskcost run --mock        # full pipeline with simulated models, no API key or credit needed
```

For a live run, put a SovereignEG key in `.env` (`LLM_API_KEY`, optionally `LLM_BASE_URL`), then:

```bash
uv run taskcost models            # current model ids and EGP prices
uv run taskcost validate --live   # calibrate the judge panel: references must pass, counterexamples must fail
uv run taskcost run               # uses benchmark.yaml
uv run taskcost run --only reasoning --trials 1   # quick, cheap subset
uv run taskcost report results/<run-dir>          # rebuild reports from saved attempts
```

Each run writes a folder under `results/` containing:

| File | Contents |
|---|---|
| `config.json` | Settings, task ids and the catalog prices captured at start |
| `attempts.jsonl` | One line per attempt: output, tokens, cost, latency, grade, each judge's verdict per criterion |
| `report.md` | Leaderboard, per-category results, judge agreement and the most common failures |
| `leaderboard.csv`, `summary.json` | The same results for spreadsheets and dashboards |

## Methodology

- **Run**: one model on one task for one trial. A run may make up to `max_attempts` attempts (the same prompt again after a failed grade), and every attempt is billed to the run.
- **Success rate**: share of runs that passed. **Reliability**: share of tasks that passed in *every* trial.
- **Ranking**: models are ranked by cost per successful task, but only if their success rate is at least `min_success_rate` (80% by default). Below that, a model is listed but not ranked; otherwise a model that only solves easy tasks would look cheapest.
- **Cost**: computed from token usage and the catalog's EGP prices (input, output and cached input). Reasoning tokens are billed as output, so long hidden reasoning shows up in the cost.
- **Grading is done by AI judges.** Each task lists criteria in plain language, such as "states the total as 29,680 EGP". A panel of judge models from different families checks every criterion, judging meaning rather than wording or format, so a correct answer passes whether it is a table, a list or a paragraph. Each criterion passes on a strict majority vote. A task passes when every required criterion passes. The judges see a reference answer as one example of a correct answer.
- **Judge calibration**: every task carries counterexamples (plausible wrong answers). `taskcost validate --live` checks that the panel passes the reference and rejects every counterexample. Run it after changing tasks or judges. The report also shows how often the judges were unanimous; low agreement points to vague criteria.
- **Judge independence**: judge spend is reported separately and never charged to the models under test. The runner warns if a judge is also a model under test, because judges tend to favour their own answers.
- **Errors**: API failures count as failed attempts and are reported as an error rate. Runs where the *grader* failed are excluded and listed in the notes.
- **Reproducibility**: prices, settings, raw outputs and every judge's per-criterion verdict are saved with every run, so anyone can audit a grade or rebuild the report with `taskcost report`. AI grading varies slightly between runs; trials and the majority vote keep that variation small.

Read results per category as well as overall: cost per success is most comparable between models with similar success rates.

## Adding tasks

Tasks are YAML files in `tasks/`. Each needs `id`, `category`, `prompt`, `grader`, a `reference` answer and a few `counterexamples`.

```yaml
- id: extraction-po-number
  category: extraction
  prompt: |
    What is the purchase order number in this email, and who approved it?
    ...
  grader:
    type: rubric
    criteria:
      - Gives the purchase order number as PO-88213.
      - Names Hala Mansour as the approver.
      - text: Mentions the approval date.
        required: false
  reference: The PO number is PO-88213, approved by Hala Mansour on 2 October.
  counterexamples:
    - The PO number is PO-88231, approved by Hala Mansour.
    - The PO number is PO-88213, approved by the finance team.
```

Writing good criteria:

- **One fact per criterion.** "Does not flag C1, C3 or C6" let a wrong answer through during calibration; three separate criteria caught it.
- **Say what matters and allow any form:** "gives the due date as 14 October 2026, in any date format".
- **Say why when there's a trap:** "routes T1 to technical (it reports an app crash, even though it mentions charges)".
- **Use real mistakes as counterexamples.** Wrong answers that models actually gave are the best test of the judges.

Grader types:

- `rubric` (default): judge panel over criteria, as above. `include_reference` defaults to true.
- `llm_judge`: a single overall `rubric` sentence; a one-criterion `rubric`.
- Hard checks for when a strict format really is the requirement: `json_fields` (JSON field values, paths like `items[*].owner`, `{any_of: [...]}`, `{superset: [...]}`), `contains_all`, `exact`, `regex` and `max_words`.
- `all_of` combines graders in order and stops at the first failure, so put free hard checks before the paid judges.

## Configuration

See `benchmark.yaml`. Each model can be a plain id or an object with a `label` and extra request parameters, which lets you compare settings of the same model:

```yaml
models:
  - gemini-3.1-flash-lite
  - id: gpt-oss-120b-turbo
    label: gpt-oss-120b-turbo@low
    reasoning_effort: low
```

## Tests

```bash
uv run pytest
```
