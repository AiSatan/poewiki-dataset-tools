# poewiki-dataset

Tools for dumping current Path of Exile Wiki page wikitext into JSON Lines.

This repository is intended for the Python dump script. The generated wiki data
belongs in a separate Hugging Face dataset repository, not in this GitHub code
repository.

## Usage

```bash
python3 scripts/dump_poewiki.py --output data/poewiki_pages.jsonl
```

Resume an interrupted dump:

```bash
python3 scripts/dump_poewiki.py --output data/poewiki_pages.jsonl --resume
```

Write a small sample:

```bash
python3 scripts/dump_poewiki.py --output data/poewiki_pages.jsonl --limit 100
```

## Output Schema

Each line is a JSON object:

| Field | Type | Description |
| --- | --- | --- |
| `pageid` | integer | MediaWiki page ID. |
| `revid` | integer | Revision ID for the dumped wikitext. |
| `timestamp` | string | Revision timestamp from the wiki API. |
| `title` | string | Page title. |
| `wikitext` | string | Raw MediaWiki wikitext for the page. |

## Licensing

The Python code in this repository is licensed under the MIT License. See
[LICENSE](LICENSE).

The generated dataset contains PoE Wiki content. The PoE Wiki copyright page
states that textual and graphical content that PoE Wiki may lawfully license is
licensed under Creative Commons Attribution-NonCommercial-ShareAlike 3.0
Unported. Use the Hugging Face license identifier `cc-by-nc-sa-3.0` for the
dataset repository.

This project is not affiliated with, endorsed by, sponsored by, or approved by
Grinding Gear Games.
