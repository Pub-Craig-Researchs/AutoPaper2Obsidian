# AutoPaper2Obsidian

> Automated academic paper processing pipeline for Obsidian knowledge base

AutoPaper2Obsidian converts raw academic PDFs into clean, interlinked Markdown notes inside your Obsidian vault — fully automated, from parsing to archiving.

## Features

- **MinerU-powered PDF parsing** — hybrid-auto-engine for highest layout accuracy (tables, figures, equations)
- **LLM metadata extraction** — title, author, year, journal, DOI via structured JSON Schema
- **Three-tier reference splitting** — heuristic regex + density validation + LLM fallback
- **Obsidian bilink reconstruction** — replaces `[1]`-style citations with `[[Author_Year_ShortTitle]]` wikilinks
- **YAML Frontmatter injection** — Dataview-compatible metadata block
- **Automatic image migration** — copies figures to Obsidian attachments with path rewriting
- **Transaction-safe file operations** — atomic moves prevent partial writes
- **JSON Lines processing log** — full audit trail in `mapping_log.jsonl`

## Prerequisites

| Requirement | Details                                                    |
| ----------- | ---------------------------------------------------------- |
| Python      | 3.13+                                                      |
| MinerU      | Local GPU deployment, running at `http://localhost:8866` |
| LLM API     | DeepSeek recommended; any OpenAI-compatible endpoint works |
| Obsidian    | With Dataview plugin enabled                               |

## Quick Start

1. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```
2. **Configure API keys**

   Edit `api_config.json` and replace the placeholder API key with your real key:

   ```json
   "API_KEY": "sk-your-deepseek-api-key-here"
   ```
3. **Set your paths** in `src/config.py`:

   | Variable                 | Description                                            |
   | ------------------------ | ------------------------------------------------------ |
   | `PROJECT_ROOT`         | Project root directory                                 |
   | `OBSIDIAN_VAULT`       | Path to your Obsidian vault                            |
   | `OBSIDIAN_ATTACHMENTS` | Attachments subdirectory inside the vault              |
   | `MINERU_API`           | MinerU service URL (default `http://localhost:8866`) |
4. **Start MinerU** service
5. **Place PDFs** in the `papers_raw/` folder
6. **Run**

   ```bash
   python run.py
   ```

## Usage

```bash
# Batch process all PDFs in papers_raw/
python run.py

# Process a single file
python run.py --file paper.pdf

# Dry run — parse only, no migration to Obsidian
python run.py --dry-run
```

## Project Structure

```
.
├── README.md                       # This file
├── run.py                          # Entry point
├── requirements.txt                # Python dependencies
├── api_config.json                 # LLM API configuration (multi-profile)
├── src/
│   ├── __init__.py
│   ├── config.py                   # Paths & service URLs
│   ├── pipeline.py                 # Main orchestration pipeline
│   ├── mineru_client.py            # MinerU PDF parsing client
│   ├── llm_client.py               # LLM API client (OpenAI-compatible)
│   ├── metadata_extractor.py       # Structured metadata extraction
│   ├── text_cleaner.py             # Markdown cleaning & reference splitting
│   ├── archiver.py                 # Obsidian archival & bilink injection
│   └── utils.py                    # Logging, directory helpers
├── prompts/
│   └── metadata_extract.txt        # LLM prompt template
└── deepseek_v3_tokenizer/
    ├── tokenizer.json
    ├── tokenizer_config.json
    └── deepseek_tokenizer.py       # Token counting for cost estimation
```

## Processing Pipeline

The pipeline processes each PDF through **8 sequential steps**:

| Step | Action                                                                            | Module                    |
| ---: | --------------------------------------------------------------------------------- | ------------------------- |
|    1 | **MinerU PDF parsing** → Markdown + images                                 | `mineru_client.py`      |
|    2 | **Read largest Markdown** file from MinerU output                           | `pipeline.py`           |
|    3 | **LLM metadata extraction** from first 3 000 chars                          | `metadata_extractor.py` |
|    4 | **Generate standard filename**: `{FirstAuthor}_{Year}_{ShortTitle}`       | `metadata_extractor.py` |
|    5 | **Text cleaning** — fix line breaks, remove headers/footers                | `text_cleaner.py`       |
|    6 | **Reference splitting** — 3-tier strategy (heuristic → density → LLM)    | `text_cleaner.py`       |
|    7 | **Bilink reconstruction** — replace `[1]` with `[[Author_Year_Title]]` | `archiver.py`           |
|    8 | **Archive to Obsidian** + write log entry                                   | `archiver.py`           |

## Deduplication

- Automatically skips PDFs already recorded in `mapping_log.jsonl`
- Only **successful** processing is logged
- Use `--file` to force-reprocess a specific PDF

## Configuration

### `api_config.json`

Supports multiple LLM profiles. Set `active_profile` to switch between providers:

```json
{
    "active_profile": "deepseek",
    "profiles": {
        "deepseek": { "API_KEY": "...", "BASE_URL": "...", "MODEL": "deepseek-chat" },
        "openai":   { "API_KEY": "...", "BASE_URL": "...", "MODEL": "gpt-xx" },
        "local_vllm": { "API_KEY": "sk-no-key-needed", "BASE_URL": "http://localhost:8000/v1", "MODEL": "..." }
    }
}
```

### `src/config.py`

All file paths and service URLs are centralized here. Update before first run.

## Performance Tuning

| Parameter                    | Location             | Default           | Description                       |
| ---------------------------- | -------------------- | ----------------- | --------------------------------- |
| `N_PARALLELS`              | `api_config.json`  | 20                | Max concurrent LLM requests       |
| `max_wait`                 | `mineru_client.py` | 2400 s            | MinerU polling timeout (40 min)   |
| `MODEL`                    | `api_config.json`  | `deepseek-chat` | LLM model selection               |
| `input_price_per_million`  | `api_config.json`  | 2                 | Cost tracking: input token price  |
| `output_price_per_million` | `api_config.json`  | 3                 | Cost tracking: output token price |

## Fault Tolerance

| Failure                     | Behavior                                            |
| --------------------------- | --------------------------------------------------- |
| MinerU parsing failure      | Skip PDF, continue to next                          |
| LLM hallucination           | JSON Schema validation + retry + timestamp fallback |
| Reference splitting failure | Preserve original text unchanged                    |
| API rate limit              | Exponential backoff retry                           |

## Manual Reference Formatting Prompt

If some processed Markdown files have missing or broken reference sections, you can manually copy the references from the original PDF and use the following LLM prompt to format them into Obsidian bilinks:

<details>
<summary>Click to expand the prompt</summary>

```
Role: You are a professional academic metadata extraction expert, skilled at
converting complex reference entries into structured Wiki-link format.

Task: Convert the provided reference list into double-bracket link format.

Formatting Rules (strictly follow):

1. Structure: [[FirstAuthor_Year_TitleFragment]]

2. First Author: Keep only the first author's surname. If it includes
   prefixes like "van" or "de", keep them (e.g., van_Binsbergen).

3. Year: Extract the publication year.

4. Title Fragment:
   - Extract the first 10 words of the title. If the title has fewer
     than 10 words, use the entire title.
   - Remove all punctuation (commas, periods, colons, question marks,
     quotation marks, parentheses, etc.).
   - Replace all spaces and hyphens (-) with underscores (_).

5. Cleanup: Ensure no consecutive underscores and no leading/trailing
   underscores in the final string.

6. Output: One converted link per line. Do not include the original
   citation text or any explanation.

Example:

Input:
Campbell, J. Y. and S. B. Thompson (2008). Predicting excess stock
returns out of sample: Can anything beat the historical average?
The Review of Financial Studies 21 (4), pp. 1509-1531.

Output:
[[Campbell_2008_Predicting_excess_stock_returns_out_of_sample_Can_anything_beat]]
```

</details>

## License

MIT

## Credits

- [MinerU](https://github.com/opendatalab/MinerU) — PDF parsing engine
- [Obsidian](https://obsidian.md/) — Knowledge base
- [Rich](https://github.com/Textualize/rich) — Terminal output formatting
