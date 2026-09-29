# Plugin eval suite (`claude plugin eval`)

Fast, mocked checks of the skills' behaviour: the coach's MCP tools return canned results from
`mocks/coach/`, so no pack, models or Founder data are involved. The real-host gates (G2, G4,
G5, G6 on the real server and pack) are `ytbrain eval coach`.

```bash
python scripts/assemble_plugin.py --pack data/pack --with-evals --out dist/plugin-eval
claude plugin eval dist/plugin-eval --runs 3 --threshold 0.8
```

Every miss found while dogfooding becomes a case here.

`mocks/coach/_tools.json` gives the mocked server the real tool schemas, descriptions and hints;
the assembler rewrites it from `tests/golden/coach_tools.json` in every `--with-evals` build.

Reports are published to claude.ai (private to you) by default; add `--no-publish` to keep them local.
